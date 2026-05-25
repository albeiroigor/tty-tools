#!/usr/bin/env python3
"""
Network-TUI (nwktui) — Interfaz TUI para gestión de redes en dispositivos sin entorno grafico
Parte de tty-tools | Requiere: python3, NetworkManager (nmcli), iproute2
Uso: python3 nwktui.py
"""

import curses
import subprocess
import threading
import time
import re
import sys
import os
import shlex
import shutil

# ─── Colores (heredan del tema de terminal) ────────────────────────────────
C_DEFAULT  = 0
C_HEADER   = 1
C_SELECTED = 2
C_GREEN    = 3
C_RED      = 4
C_YELLOW   = 5
C_CYAN     = 6
C_BLUE     = 7
C_GRAY     = 8
C_TITLE    = 9
C_BORDER   = 10

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER,   curses.COLOR_BLACK,   curses.COLOR_BLUE)
    curses.init_pair(C_SELECTED, curses.COLOR_BLACK,   curses.COLOR_CYAN)
    curses.init_pair(C_GREEN,    curses.COLOR_GREEN,   -1)
    curses.init_pair(C_RED,      curses.COLOR_RED,     -1)
    curses.init_pair(C_YELLOW,   curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_CYAN,     curses.COLOR_CYAN,    -1)
    curses.init_pair(C_BLUE,     curses.COLOR_BLUE,    -1)
    curses.init_pair(C_GRAY,     curses.COLOR_WHITE,   -1)  # gris claro
    curses.init_pair(C_TITLE,    curses.COLOR_CYAN,    -1)  # alias
    curses.init_pair(C_BORDER,   curses.COLOR_BLUE,    -1)

# ─── Helpers seguros ────────────────────────────────────────────────────────
def run_cmd(cmd_list, timeout=5):
    """Ejecuta un comando como lista (sin shell=True), captura stdout."""
    try:
        r = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode, r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", 1, "timeout"
    except Exception as e:
        return "", 1, str(e)



# ─── Parseo robusto de redes WiFi ──────────────────────────────────────────
def get_wifi_list(iface=None):
    """Fuerza rescan activo y luego lista redes con timeout generoso."""
    # 1) Pedir rescan al hardware (puede tardar, ignoramos error si no hay permisos)
    rescan_cmd = ["nmcli", "device", "wifi", "rescan"]
    if iface:
        rescan_cmd += ["ifname", iface]
    run_cmd(rescan_cmd, timeout=12)
    # 2) Esperar a que el driver actualice resultados
    time.sleep(2)
    # 3) Listar con timeout generoso
    list_cmd = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list"]
    if iface:
        list_cmd += ["ifname", iface]
    out, rc, _ = run_cmd(list_cmd, timeout=15)
    nets = []
    if rc != 0 or not out:
        return nets
    lines = out.splitlines()
    for line in lines:
        if not line:
            continue
        # Separar por ':' respetando escapes \:  y \\
        parts = split_escaped(line, ':')
        if len(parts) < 4:
            continue
        ssid, signal_str, sec, freq = parts[0], parts[1], parts[2], parts[3]
        ssid = ssid.replace("\\:", ":").replace("\\\\", "\\")  # desescapa
        if ssid == '--' or not ssid.strip():
            ssid = '(oculta)'
        try:
            signal = int(signal_str) if signal_str.isdigit() else 0
        except:
            signal = 0
        band = '5 GHz' if '5' in freq else '2.4 GHz'
        nets.append({'ssid': ssid, 'signal': signal, 'security': sec, 'band': band})
    # deduplicar por SSID
    seen = set()
    uniq = []
    for n in nets:
        if n['ssid'] not in seen:
            seen.add(n['ssid'])
            uniq.append(n)
    return sorted(uniq, key=lambda x: -x['signal'])

def split_escaped(text, delim):
    """Divide 'text' por 'delim' respetando escapes con backslash."""
    parts = []
    current = []
    i = 0
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text):
            current.append(text[i+1])
            i += 2
        elif text[i] == delim:
            parts.append(''.join(current))
            current = []
            i += 1
        else:
            current.append(text[i])
            i += 1
    parts.append(''.join(current))
    return parts

# ─── Información de interfaz (cacheada) ─────────────────────────────────────
def get_iface_info(iface):
    """Ejecuta comandos ip y devuelve dict con IP, MAC, gateway."""
    info = {}
    out, _, _ = run_cmd(["ip", "addr", "show", iface])
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+)', out)
    if m:
        info['ip'] = m.group(1)
        info['mask'] = m.group(2)
    m = re.search(r'link/ether ([\da-f:]+)', out)
    if m:
        info['mac'] = m.group(1)
    # gateway
    out2, _, _ = run_cmd(["ip", "route", "show", "dev", iface, "default"])
    m2 = re.search(r'default via (\d+\.\d+\.\d+\.\d+)', out2)
    if not m2:
        out3, _, _ = run_cmd(["ip", "route"])
        m2 = re.search(r'default via (\d+\.\d+\.\d+\.\d+)', out3)
    if m2:
        info['gateway'] = m2.group(1)
    return info

def get_active_conn():
    """Conexiones activas (nmcli -t)."""
    out, rc, _ = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,STATE", "connection", "show", "--active"])
    conns = []
    if rc != 0:
        return conns
    for line in out.splitlines():
        parts = split_escaped(line, ':')
        if len(parts) >= 4:
            conns.append({'name': parts[0], 'type': parts[1], 'device': parts[2], 'state': parts[3]})
    return conns

def get_wifi_signal(iface):
    """Devuelve info de la red conectada en una interfaz wifi."""
    cmd = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", "ifname", iface]
    out, _, _ = run_cmd(cmd)
    for line in out.splitlines():
        if line.startswith('*'):
            parts = split_escaped(line, ':')
            if len(parts) < 5:
                continue
            ssid = parts[1].replace("\\:", ":").replace("\\\\", "\\")
            signal = int(parts[2]) if parts[2].isdigit() else 0
            sec = parts[3]
            freq = parts[4]
            band = '5 GHz' if '5' in freq else '2.4 GHz'
            return {'ssid': ssid, 'signal': signal, 'security': sec, 'band': band}
    return {}

def get_dns():
    """Obtiene servidores DNS sin shell=True. Devuelve lista de strings."""
    results = []
    # Intento 1: resolvectl
    out, rc, _ = run_cmd(["resolvectl", "status"])
    if rc == 0:
        for line in out.splitlines():
            if "DNS Servers" in line:
                # "DNS Servers: 1.1.1.1 8.8.8.8" → extraer IPs
                parts = line.split(":", 1)
                if len(parts) == 2:
                    results.extend(parts[1].strip().split())
    if results:
        return results
    # Intento 2: nmcli
    out, rc, _ = run_cmd(["nmcli", "dev", "show"])
    if rc == 0:
        for line in out.splitlines():
            if "IP4.DNS" in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    results.append(parts[1].strip())
    return results if results else ["No detectado"]

def get_interfaces():
    out, _, _ = run_cmd(["ip", "link", "show"])
    ifaces = re.findall(r'^\d+: (\S+):', out, re.MULTILINE)
    return [i.rstrip(':') for i in ifaces if i not in ('lo',)]

def sig_bars(pct):
    lvls = [20, 40, 60, 80]
    bar = ""
    for l in lvls:
        bar += "█" if pct >= l else "░"
    return bar

def sig_color(pct):
    if pct >= 65: return C_GREEN
    if pct >= 40: return C_YELLOW
    return C_RED

def ping_host(host, count=5):
    cmd = ["ping", "-c", str(count), "-W", "1", host]
    out, rc, _ = run_cmd(cmd, timeout=count + 3)
    return out, rc

def get_saved_connections():
    """SSID de redes WiFi guardadas en NetworkManager."""
    out, rc, _ = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    saved = set()
    if rc != 0:
        return saved
    for line in out.splitlines():
        parts = line.split(':', 1)
        if len(parts) == 2 and 'wireless' in parts[1]:
            saved.add(parts[0].strip())
    return saved

# ─── Aplicación principal ──────────────────────────────────────────────────
class App:
    TABS = ["Conexión", "DNS", "Ping"]

    def __init__(self, stdscr):
        self.scr = stdscr
        self.tab = 0
        self.wifi_list = []
        self.wifi_sel = 0
        self.ifaces = []
        self.iface_sel = 0
        self.status_msg = ""
        self.status_ok = True
        self.ping_log = []
        self.ping_host = "1.1.1.1"
        self.modal = None
        self.modal_fields = []       # elementos: (label, value, is_password)
        self.modal_focus = 0
        self.modal_cb = None
        self.modal_title = ""
        self.last_active = {}
        self.wifi_info = {}
        # caché para info de interfaz (evita comandos en cada frame)
        self.iface_cache = {}
        self.dns_cache = []        # caché DNS (evita llamadas lentas en cada frame)
        self.dns_cache_ts = 0      # timestamp de la última actualización
        self.loading = False
        self.scanning = False
        self.saved_connections = set()
        self.lock = threading.Lock()
        curses.curs_set(0)
        self.scr.nodelay(True)
        self.scr.timeout(200)
        init_colors()
        self.refresh_data()

    def refresh_data(self):
        """Actualiza datos en segundo plano (seguro para hilos)."""
        ifaces = get_interfaces() or ['wlan0']
        active = {c['device']: c for c in get_active_conn()}
        wifi_info = {}
        if ifaces:
            iface = ifaces[self.iface_sel % len(ifaces)] if ifaces else 'wlan0'
            if 'w' in iface:
                wifi_info = get_wifi_signal(iface)
        with self.lock:
            self.ifaces = ifaces
            self.last_active = active
            self.wifi_info = wifi_info
            # actualizar caché de IP para las interfaces visibles
            for iface in ifaces:
                if iface not in self.iface_cache:
                    self.iface_cache[iface] = get_iface_info(iface)
            self.saved_connections = get_saved_connections()

    def current_iface(self):
        with self.lock:
            if not self.ifaces:
                return 'wlan0'
            return self.ifaces[self.iface_sel % len(self.ifaces)]

    def set_status(self, msg, ok=True):
        with self.lock:
            self.status_msg = msg
            self.status_ok = ok

    # ── Dibujo ────────────────────────────────────────────────────────────
    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 10 or w < 20:
            self.scr.addstr(0, 0, "Terminal muy pequeña")
            self.scr.refresh()
            return

        self.draw_titlebar(w)
        self.draw_tabs(w)
        self.draw_sidebar(h, w)
        self.draw_content(h, w)
        self.draw_statusbar(h, w)

        if self.modal:
            self.draw_modal(h, w)

        self.scr.refresh()

    def safe_addstr(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        max_len = w - x - 1
        if max_len <= 0:
            return
        try:
            self.scr.addstr(y, x, text[:max_len], attr)
        except curses.error:
            pass

    def draw_titlebar(self, w):
        bar = "Network-TUI — tty-tools"
        self.scr.attron(curses.color_pair(C_HEADER) | curses.A_BOLD)
        self.scr.addstr(0, 0, bar.ljust(w - 1))
        t = time.strftime("%H:%M:%S")
        self.safe_addstr(0, w - len(t) - 2, t)
        self.scr.attroff(curses.color_pair(C_HEADER) | curses.A_BOLD)

    def draw_tabs(self, w):
        x = 0
        for i, tab in enumerate(self.TABS):
            label = f" {tab} "
            if i == self.tab:
                attr = curses.color_pair(C_CYAN) | curses.A_BOLD | curses.A_UNDERLINE
            else:
                attr = curses.color_pair(C_GRAY)
            self.safe_addstr(1, x, label, attr)
            x += len(label) + 1
        # línea debajo de tabs
        self.safe_addstr(1, x, "─" * (w - x - 1), curses.color_pair(C_GRAY))

    def draw_sidebar(self, h, w):
        sw = 18
        # fondo claro
        for y in range(2, h - 1):
            self.safe_addstr(y, 0, " " * sw)
        # bordes
        self.safe_addstr(2, 0, "┌" + "─" * (sw-2) + "┐", curses.color_pair(C_BORDER))
        for y in range(3, h-2):
            self.safe_addstr(y, 0, "│", curses.color_pair(C_BORDER))
            self.safe_addstr(y, sw-1, "│", curses.color_pair(C_BORDER))
        self.safe_addstr(h-2, 0, "└" + "─" * (sw-2) + "┘", curses.color_pair(C_BORDER))
        # título
        self.safe_addstr(2, 2, " Interfaces ", curses.color_pair(C_BLUE) | curses.A_BOLD)
        # lista interfaces
        for i, iface in enumerate(self.ifaces):
            y = 3 + i
            if y >= h - 3:
                break
            active = iface in self.last_active
            dot = "●" if active else "○"
            col = C_GREEN if active else C_GRAY
            if i == self.iface_sel:
                self.safe_addstr(y, 1, f" {dot} {iface:<13}", curses.color_pair(C_SELECTED) | curses.A_BOLD)
            else:
                self.safe_addstr(y, 2, dot, curses.color_pair(col))
                self.safe_addstr(y, 4, iface[:13])
        # acciones rápidas
        y_base = 4 + len(self.ifaces)
        if y_base < h - 3:
            self.safe_addstr(y_base, 1, "┌ Acciones " + "─" * (sw - 14) + "┐", curses.color_pair(C_BORDER))
            acciones = [
                ("space", "Conectar"),
                ("d", "Desconectar"),
                ("s", "Escanear"),
                ("i", "IP fija"),
                ("n", "DNS"),
                ("p", "Ping"),
                ("←→", "Interfaz"),
            ]
            for j, (key, label) in enumerate(acciones):
                y = y_base + 1 + j
                if y >= h - 3:
                    break
                self.safe_addstr(y, 1, f" [{key}] {label}"[:sw-2], curses.color_pair(C_GRAY))
        # línea vertical separadora
        for y in range(2, h - 1):
            self.safe_addstr(y, sw, "│", curses.color_pair(C_BORDER))

    def draw_content(self, h, w):
        sw = 19
        cw = w - sw - 1
        if self.tab == 0:
            self.draw_conn(h, w, sw, cw)
        elif self.tab == 1:
            self.draw_dns(h, w, sw, cw)
        elif self.tab == 2:
            self.draw_ping(h, w, sw, cw)

    def draw_section(self, y, x, title, w):
        self.safe_addstr(y, x, "┌─ " + title + " " + "─" * (w - len(title) - 5) + "┐", curses.color_pair(C_BORDER))

    def draw_kv(self, y, x, key, val, val_color=0, w=40):
        self.safe_addstr(y, x, f"{key:<14}", curses.color_pair(C_GRAY))
        self.safe_addstr(y, x + 14, str(val)[:w - 15], curses.color_pair(val_color) if val_color else 0)

    # ── Tab Conexión (WiFi + Ethernet unificados) ─────────────────────────
    def draw_conn(self, h, w, sx, cw):
        x = sx
        y = 2

        # ── Selector de interfaz inline ───────────────────────────────────
        ifaces = self.ifaces
        if not ifaces:
            self.safe_addstr(y+1, x+2, "No se detectaron interfaces.", curses.color_pair(C_YELLOW))
            return

        # barra de selección horizontal
        self.safe_addstr(y, x, " Interfaz: ", curses.color_pair(C_BLUE) | curses.A_BOLD)
        bx = x + 11
        for i, iface in enumerate(ifaces):
            active = iface in self.last_active
            dot = "●" if active else "○"
            dot_col = C_GREEN if active else C_RED
            label = f" {dot} {iface} "
            if i == self.iface_sel:
                self.safe_addstr(y, bx, label, curses.color_pair(C_SELECTED) | curses.A_BOLD)
            else:
                self.safe_addstr(y, bx, f" {dot} ", curses.color_pair(dot_col))
                self.safe_addstr(y, bx + 3, f"{iface} ", curses.color_pair(C_GRAY))
            bx += len(label) + 1
        self.safe_addstr(y, bx, " [←→] cambiar", curses.color_pair(C_GRAY))
        y += 1

        iface = self.current_iface()
        is_wifi = 'w' in iface

        # info de interfaz
        if iface not in self.iface_cache:
            self.iface_cache[iface] = get_iface_info(iface)
        info = self.iface_cache[iface]
        active = iface in self.last_active
        estado = ("● Conectado", C_GREEN) if active else ("○ Desconectado", C_RED)

        # ── Sección estado ────────────────────────────────────────────────
        tipo = "WiFi" if is_wifi else "Ethernet"
        self.draw_section(y, x, f" {tipo} — {iface} ", cw); y += 1

        if is_wifi:
            winfo = self.wifi_info if iface == self.current_iface() else get_wifi_signal(iface)
            ssid  = winfo.get('ssid', '—')
            sig   = winfo.get('signal', 0)
            sec   = winfo.get('security', '—')
            band  = winfo.get('band', '—')
            self.draw_kv(y, x+1, "SSID",      ssid,          C_CYAN, cw); y += 1
            self.draw_kv(y, x+1, "Estado",    estado[0],     estado[1], cw); y += 1
            self.draw_kv(y, x+1, "IP",        info.get('ip','—'), C_BLUE, cw); y += 1
            self.draw_kv(y, x+1, "Máscara",   f"/{info.get('mask','—')}", 0, cw); y += 1
            self.draw_kv(y, x+1, "Gateway",   info.get('gateway','—'), 0, cw); y += 1
            self.draw_kv(y, x+1, "MAC",       info.get('mac','—'), C_GRAY, cw); y += 1
            self.draw_kv(y, x+1, "Seguridad", sec,           C_YELLOW, cw); y += 1
            self.draw_kv(y, x+1, "Banda",     band,          0, cw); y += 1
            # barra de señal
            bars = sig_bars(sig)
            self.safe_addstr(y, x+1, f"{'Señal':<14}", curses.color_pair(C_GRAY))
            self.safe_addstr(y, x+15, bars, curses.color_pair(sig_color(sig)))
            self.safe_addstr(y, x+20, f" {sig}%", curses.color_pair(sig_color(sig)))
            y += 2
        else:
            # Ethernet
            self.draw_kv(y, x+1, "Estado",  estado[0],              estado[1], cw); y += 1
            self.draw_kv(y, x+1, "IP",      info.get('ip','—'),     C_BLUE, cw); y += 1
            self.draw_kv(y, x+1, "Máscara", f"/{info.get('mask','—')}", 0, cw); y += 1
            self.draw_kv(y, x+1, "Gateway", info.get('gateway','—'), 0, cw); y += 1
            self.draw_kv(y, x+1, "MAC",     info.get('mac','—'),    C_GRAY, cw); y += 1
            y += 2

        # ── Lista redes WiFi (solo si la interfaz es wireless) ────────────
        if is_wifi and h - y > 6:
            self.draw_section(y, x, " Redes disponibles (↑↓, Enter) ", cw); y += 1
            self.safe_addstr(y, x, f"  {'SSID':<24} {'':1} {'Señal':>6} {'Banda':>7} {'Seg':>6}",
                             curses.color_pair(C_BLUE) | curses.A_BOLD)
            y += 1
            nets = self.wifi_list
            for i, net in enumerate(nets):
                if y >= h - 2:
                    break
                ssid_n  = net['ssid'][:23]
                sig_n   = net['signal']
                bar_n   = sig_bars(sig_n)
                sec_n   = "🔒" if net['security'] not in ('--', '', ' ') else "  "
                band_n  = net['band']
                saved_n = "★" if net['ssid'] in self.saved_connections else " "
                line    = f"  {ssid_n:<24}{saved_n} {bar_n} {sig_n:>3}% {band_n:>7} {sec_n}"
                col    = sig_color(sig_n)
                if i == self.wifi_sel:
                    self.safe_addstr(y, x, line[:cw], curses.color_pair(C_SELECTED) | curses.A_BOLD)
                else:
                    self.safe_addstr(y, x, line[:cw], curses.color_pair(col))
                y += 1
            if not nets:
                if self.scanning:
                    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
                    sp = spinner[int(time.time() * 8) % len(spinner)]
                    self.safe_addstr(y, x, f"  {sp} Escaneando redes WiFi...", curses.color_pair(C_YELLOW))
                else:
                    self.safe_addstr(y, x, "  Sin redes — presiona [s] para escanear", curses.color_pair(C_GRAY))

    # ── Tab DNS ───────────────────────────────────────────────────────────
    def draw_dns(self, h, w, sx, cw):
        x = sx; y = 2
        self.draw_section(y, x, " Configuración DNS ", cw); y += 1

        # usar caché para no bloquear el frame con un comando lento
        now = time.time()
        if now - self.dns_cache_ts > 5:   # refrescar cada 5 segundos
            self.dns_cache_ts = now
            threading.Thread(target=self._refresh_dns_cache, daemon=True).start()
        dns_servers = self.dns_cache or ["Cargando..."]

        self.safe_addstr(y, x+1, "DNS activos:", curses.color_pair(C_BLUE) | curses.A_BOLD); y += 1
        for srv in dns_servers:
            self.safe_addstr(y, x+3, srv[:cw-4], curses.color_pair(C_CYAN)); y += 1
        y += 1

        self.draw_section(y, x, " Presets rápidos ", cw); y += 1
        presets = [
            ("[1] Cloudflare", "1.1.1.1 / 1.0.0.1"),
            ("[2] Google",     "8.8.8.8 / 8.8.4.4"),
            ("[3] Quad9",      "9.9.9.9 / 149.112.112.112"),
            ("[4] OpenDNS",    "208.67.222.222 / 208.67.220.220"),
        ]
        for label, val in presets:
            self.safe_addstr(y, x+2, f"{label:<20}", curses.color_pair(C_YELLOW))
            self.safe_addstr(y, x+22, val, curses.color_pair(C_GRAY))
            y += 1
        y += 1
        self.safe_addstr(y, x+1, "[n] Editar DNS   [f] Limpiar caché   [1-4] Presets", curses.color_pair(C_CYAN))

    # ── Tab Ping ──────────────────────────────────────────────────────────
    def draw_ping(self, h, w, sx, cw):
        x = sx; y = 2
        self.draw_section(y, x, f" Ping — {self.ping_host} ", cw); y += 1
        self.safe_addstr(y, x+1, "[p] Ping  [h] Cambiar host  [g] Gateway  [4] 1.1.1.1  [8] 8.8.8.8", curses.color_pair(C_CYAN))
        y += 2
        self.draw_section(y, x, " Resultado ", cw); y += 1
        for line in self.ping_log[-(h - y - 3):]:
            col = C_GREEN if 'bytes from' in line else C_RED if ('timeout' in line or 'error' in line.lower()) else C_GRAY
            self.safe_addstr(y, x+1, line[:cw-2], curses.color_pair(col))
            y += 1
            if y >= h - 2:
                break

    # ── Statusbar ─────────────────────────────────────────────────────────
    def draw_statusbar(self, h, w):
        iface = self.current_iface()
        active = iface in self.last_active
        conn_name = self.last_active.get(iface, {}).get('name', '—')
        left = f" ▊ {iface}  {conn_name}  "
        keys = " q:Salir  Tab:Tabs  ↑↓:Nav  Enter:Acción  ?:Ayuda "
        with self.lock:
            msg = self.status_msg[:w - len(left) - len(keys) - 2] if self.status_msg else ""
        self.scr.attron(curses.color_pair(C_HEADER) | curses.A_BOLD)
        self.scr.addstr(h-1, 0, (left + msg).ljust(w-1))
        self.scr.attroff(curses.color_pair(C_HEADER) | curses.A_BOLD)
        self.safe_addstr(h-1, w - len(keys) - 1, keys, curses.color_pair(C_HEADER))

    # ── Modal mejorado (contraseña oculta) ─────────────────────────────────
    def open_modal(self, title, fields, cb):
        """
        fields: lista de (label, value, is_password)
        is_password: si True, se muestra como asteriscos.
        """
        self.modal = True
        self.modal_title = title
        self.modal_fields = fields  # (label, value, is_password)
        self.modal_focus = 0
        self.modal_cb = cb

    def close_modal(self):
        self.modal = None
        curses.curs_set(0)

    def draw_modal(self, h, w):
        mh = len(self.modal_fields) * 3 + 6
        mw = 50
        my = (h - mh) // 2
        mx = (w - mw) // 2

        # fondo
        for y in range(my, my + mh):
            self.safe_addstr(y, mx, " " * mw)

        # borde
        self.safe_addstr(my,        mx, "┌" + "─" * (mw-2) + "┐", curses.color_pair(C_BORDER))
        self.safe_addstr(my + mh-1, mx, "└" + "─" * (mw-2) + "┘", curses.color_pair(C_BORDER))
        for y in range(my+1, my+mh-1):
            self.safe_addstr(y, mx, "│", curses.color_pair(C_BORDER))
            self.safe_addstr(y, mx+mw-1, "│", curses.color_pair(C_BORDER))

        title = f" {self.modal_title} "
        self.safe_addstr(my, mx + (mw - len(title))//2, title, curses.color_pair(C_CYAN) | curses.A_BOLD)

        for i, (label, val, is_pass) in enumerate(self.modal_fields):
            fy = my + 2 + i * 3
            self.safe_addstr(fy, mx+2, label, curses.color_pair(C_GRAY))
            display_val = '*' * len(val) if is_pass else val
            # cursor parpadeante
            if i == self.modal_focus:
                display_val += "█"
                curses.curs_set(1)
                attr = curses.color_pair(C_SELECTED)
            else:
                attr = 0
            self.safe_addstr(fy+1, mx+2, f" {display_val:<{mw-5}}", attr)
            if i == self.modal_focus:
                curses.curs_set(0)  # se oculta al final del frame, o se pone solo en campo activo
        # instrucciones
        hint = " Tab=siguiente  Enter=confirmar  Esc=cancelar "
        self.safe_addstr(my+mh-2, mx+1, hint[:mw-2], curses.color_pair(C_GRAY))

    def modal_key(self, key):
        if key == 27:  # Esc
            self.close_modal()
            return
        if key == 9:  # Tab
            self.modal_focus = (self.modal_focus + 1) % len(self.modal_fields)
            return
        if key in (10, 13):  # Enter
            values = [v for _, v, _ in self.modal_fields]
            self.close_modal()
            if self.modal_cb:
                self.modal_cb(values)
            return
        # editar campo
        i = self.modal_focus
        label, val, is_pass = self.modal_fields[i]
        if key in (127, curses.KEY_BACKSPACE, 8):
            val = val[:-1]
        elif 32 <= key <= 126:
            val += chr(key)
        self.modal_fields[i] = (label, val, is_pass)

    # ── Acciones seguras ─────────────────────────────────────────────────
    def action_scan(self):
        iface = self.current_iface()
        self.set_status(f"Escaneando {iface}... (puede tardar ~15s)")
        self.scanning = True
        def _scan():
            try:
                nets = get_wifi_list(iface if 'w' in iface else None)
                wifi_info = get_wifi_signal(iface)
                with self.lock:
                    self.wifi_list = nets
                    self.wifi_info = wifi_info
                if nets:
                    self.set_status(f"Scan completado — {len(nets)} redes encontradas")
                else:
                    self.set_status("Sin redes — verifica que el WiFi esté activo", ok=False)
            except Exception as e:
                self.set_status(f"Error en scan: {e}", ok=False)
            finally:
                self.scanning = False
        threading.Thread(target=_scan, daemon=True).start()

    def action_connect(self):
        sel_net = ""
        if self.tab == 0 and self.wifi_list:
            sel_net = self.wifi_list[self.wifi_sel % len(self.wifi_list)]['ssid']
        self.open_modal(
            "Conectar a WiFi",
            [("SSID", sel_net, False), ("Contraseña", "", True)],  # campo password marcado
            self._do_connect
        )

    def _do_connect(self, vals):
        ssid, pw = vals[0], vals[1]
        if not ssid:
            self.set_status("SSID vacío", ok=False); return
        self.set_status(f"Conectando a {ssid}...")
        def _c():
            cmd = ["nmcli", "device", "wifi", "connect", ssid]
            if pw:
                cmd += ["password", pw]
            out, rc, err = run_cmd(cmd, timeout=15)
            if rc == 0:
                self.set_status(f"Conectado a {ssid}")
            else:
                self.set_status(f"Error: {err or out[:60]}", ok=False)
            self.refresh_data()
        threading.Thread(target=_c, daemon=True).start()

    def action_connect_saved(self, ssid):
        """Espacio: conecta red guardada sin contraseña, o abre modal si es nueva."""
        if ssid in self.saved_connections:
            self.set_status(f"Conectando a {ssid} (guardada)...")
            def _up():
                _, rc, err = run_cmd(["nmcli", "connection", "up", ssid], timeout=15)
                if rc == 0:
                    self.set_status(f"Conectado a {ssid}")
                else:
                    self.set_status(f"Error: {err[:60]}", ok=False)
                self.refresh_data()
            threading.Thread(target=_up, daemon=True).start()
        else:
            self.open_modal(
                "Conectar",
                [("SSID", ssid, False), ("Contraseña", "", True)],
                self._do_connect
            )

    def action_disconnect(self):
        iface = self.current_iface()
        out, rc, _ = run_cmd(["nmcli", "device", "disconnect", iface])
        if rc == 0:
            self.set_status(f"Desconectado {iface}")
        else:
            self.set_status(f"Error: {out[:60]}", ok=False)
        self.refresh_data()

    def action_edit_ip(self):
        iface = self.current_iface()
        info = self.iface_cache.get(iface, get_iface_info(iface))
        self.open_modal(
            f"IP estática — {iface}",
            [("IP", info.get('ip', ''), False),
             ("Prefijo", info.get('mask', '24'), False),
             ("Gateway", info.get('gateway', ''), False)],
            self._do_set_ip
        )

    def _do_set_ip(self, vals):
        ip, prefix, gw = vals
        iface = self.current_iface()
        conns = get_active_conn()
        conn_name = next((c['name'] for c in conns if c['device'] == iface), iface)
        cmds = [
            ["nmcli", "connection", "modify", conn_name, "ipv4.addresses", f"{ip}/{prefix}"],
            ["nmcli", "connection", "modify", conn_name, "ipv4.gateway", gw],
            ["nmcli", "connection", "modify", conn_name, "ipv4.method", "manual"],
            ["nmcli", "device", "reapply", iface],
        ]
        for cmd in cmds:
            run_cmd(cmd)
        self.set_status(f"IP {ip}/{prefix} aplicada en {iface}")
        self.refresh_data()

    def action_edit_dns(self):
        self.open_modal(
            "Editar DNS",
            [("DNS primario", "1.1.1.1", False), ("DNS secundario", "8.8.8.8", False)],
            self._do_set_dns
        )

    def _do_set_dns(self, vals):
        d1, d2 = vals[0].strip(), vals[1].strip()
        ip_re = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
        if not ip_re.match(d1):
            self.set_status(f"IP inválida: {d1!r}", ok=False); return
        if d2 and not ip_re.match(d2):
            self.set_status(f"IP inválida: {d2!r}", ok=False); return
        iface = self.current_iface()
        conns = get_active_conn()
        conn_name = next((c['name'] for c in conns if c['device'] == iface), iface)
        dns_val = f"{d1} {d2}".strip() if d2 else d1
        _, rc, err = run_cmd(["nmcli", "connection", "modify", conn_name, "ipv4.dns", dns_val])
        if rc != 0:
            self.set_status(f"Error al aplicar DNS: {err[:50]}", ok=False); return
        run_cmd(["nmcli", "device", "reapply", iface])
        with self.lock:
            self.dns_cache_ts = 0   # forzar refresco en pantalla
        self.set_status(f"DNS aplicado: {dns_val}")

    def _refresh_dns_cache(self):
        servers = get_dns()
        with self.lock:
            self.dns_cache = servers
            self.dns_cache_ts = time.time()

    def action_flush_dns(self):
        run_cmd(["resolvectl", "flush-caches"])
        # forzar refresco del caché DNS en pantalla
        with self.lock:
            self.dns_cache_ts = 0
        self.set_status("Caché DNS limpiada")

    def action_ping(self):
        host = self.ping_host
        self.ping_log = [f"PING {host}..."]
        self.tab = 2
        def _ping():
            out, rc = ping_host(host)
            self.ping_log = out.splitlines() if out else ["Sin respuesta / error"]
        threading.Thread(target=_ping, daemon=True).start()

    def set_dns_preset(self, d1, d2):
        iface = self.current_iface()
        conns = get_active_conn()
        conn_name = next((c['name'] for c in conns if c['device'] == iface), iface)
        _, rc, err = run_cmd(["nmcli", "connection", "modify", conn_name, "ipv4.dns", f"{d1} {d2}"])
        if rc != 0:
            self.set_status(f"Error: {err[:50]}", ok=False); return
        run_cmd(["nmcli", "device", "reapply", iface])
        with self.lock:
            self.dns_cache_ts = 0   # forzar refresco en pantalla
        self.set_status(f"DNS → {d1} / {d2}")

    def show_help(self):
        self.ping_log = [
            "─── Atajos de teclado ───────────────────────",
            "Tab / Shift+Tab  Cambiar pestaña",
            "↑ ↓              Navegar lista de redes",
            "← →              Cambiar interfaz activa",
            "Enter            Conectar a red seleccionada",
            "c                Conectar (modal)",
            "d                Desconectar interfaz actual",
            "s                Escanear redes WiFi",
            "i                Editar IP estática",
            "n                Editar DNS",
            "p                Ping rápido al host actual",
            "h                Cambiar host de ping",
            "g                Ping al gateway",
            "4                Ping a 1.1.1.1 (Cloudflare)",
            "8                Ping a 8.8.8.8 (Google)",
            "1-4 (tab DNS)    Aplicar preset DNS",
            "f  (tab DNS)     Limpiar caché DNS",
            "r                Refrescar datos",
            "q / ESC          Salir",
        ]
        self.tab = 2

    # ── Loop principal ────────────────────────────────────────────────────
    def run(self):
        tick = 0
        while True:
            self.draw()
            key = self.scr.getch()
            tick += 1

            if tick % 50 == 0:  # cada ~10 segundos
                threading.Thread(target=self.refresh_data, daemon=True).start()

            if key == -1:
                continue

            if self.modal:
                self.modal_key(key)
                continue

            if key in (ord('q'), 27):
                break

            # ── Globales (siempre activas) ────────────────────────────────
            if key == ord('\t') or key == curses.KEY_BTAB:
                delta = 1 if key == ord('\t') else -1
                self.tab = (self.tab + delta) % len(self.TABS)
            elif key == curses.KEY_RIGHT:
                if self.ifaces:
                    self.iface_sel = (self.iface_sel + 1) % len(self.ifaces)
                    self.iface_cache.clear()
                    threading.Thread(target=self.refresh_data, daemon=True).start()
            elif key == curses.KEY_LEFT:
                if self.ifaces:
                    self.iface_sel = (self.iface_sel - 1) % len(self.ifaces)
                    self.iface_cache.clear()
                    threading.Thread(target=self.refresh_data, daemon=True).start()
            elif key == ord('r'):
                self.iface_cache.clear()
                self.refresh_data()
                self.set_status("Datos actualizados")
            elif key == ord('?'):
                self.show_help()

            # ── Tab 0: Conexión ───────────────────────────────────────────────
            elif self.tab == 0:
                if key == curses.KEY_UP:
                    self.wifi_sel = max(0, self.wifi_sel - 1)
                elif key == curses.KEY_DOWN and self.wifi_list:
                    self.wifi_sel = min(len(self.wifi_list) - 1, self.wifi_sel + 1)
                elif key == ord(' ') and self.wifi_list:
                    net = self.wifi_list[self.wifi_sel % len(self.wifi_list)]
                    self.action_connect_saved(net['ssid'])
                elif key in (10, 13) and self.wifi_list:
                    net = self.wifi_list[self.wifi_sel % len(self.wifi_list)]
                    self.open_modal(
                        "Conectar",
                        [("SSID", net['ssid'], False), ("Contraseña", "", True)],
                        self._do_connect
                    )
                elif key == ord('c'):
                    self.action_connect()
                elif key == ord('d'):
                    self.action_disconnect()
                elif key == ord('s'):
                    self.action_scan()
                elif key == ord('i'):
                    self.action_edit_ip()

            # ── Tab 1: DNS ────────────────────────────────────────────────────
            elif self.tab == 1:
                presets = {
                    ord('1'): ('1.1.1.1',       '1.0.0.1'),
                    ord('2'): ('8.8.8.8',        '8.8.4.4'),
                    ord('3'): ('9.9.9.9',        '149.112.112.112'),
                    ord('4'): ('208.67.222.222', '208.67.220.220'),
                }
                if key in presets:
                    self.set_dns_preset(*presets[key])
                elif key == ord('f'):
                    self.action_flush_dns()
                elif key == ord('n'):
                    self.action_edit_dns()

            # ── Tab 2: Ping ───────────────────────────────────────────────────
            elif self.tab == 2:
                if key == ord('p'):
                    self.action_ping()
                elif key == ord('h'):
                    self.open_modal(
                        "Host para ping",
                        [("Host / IP", self.ping_host, False)],
                        lambda v: setattr(self, 'ping_host', v[0])
                    )
                elif key == ord('g'):
                    info = self.iface_cache.get(self.current_iface(), get_iface_info(self.current_iface()))
                    self.ping_host = info.get('gateway', '192.168.1.1')
                    self.action_ping()
                elif key == ord('4'):
                    self.ping_host = '1.1.1.1'
                    self.action_ping()
                elif key == ord('8'):
                    self.ping_host = '8.8.8.8'
                    self.action_ping()

def main(stdscr):
    app = App(stdscr)
    app.run()

if __name__ == "__main__":
    if shutil.which("nmcli") is None:
        print("ERROR: nmcli no encontrado. Instala NetworkManager:")
        print("  sudo pacman -S networkmanager    # Arch/Manjaro")
        print("  sudo apt install network-manager # Debian/Ubuntu")
        sys.exit(1)
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
