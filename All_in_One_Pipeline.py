import os
import shutil
import subprocess
import time
import glob
import re
import sys
import traceback
import requests
import csv
from datetime import datetime

# =============================================================================
# 1. KONFIGURATION
# =============================================================================
TELEGRAM_TOKEN = "8589716957:AAHAAU26UrnwOWgL4OytPpmj0dSPnyWNwu0"
TELEGRAM_CHAT_ID = "711461437"

# KORRIGIERT: Name laut deinem Screenshot
LOGIC_APP_NAME = "AutoRestart-Supraleiter" 
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"

# Schwelle: Alles unter 0.05 Zuständen/eV am Fermi-Level ist ein Isolator
DOS_THRESHOLD = 0.05

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
LOG_FILE = os.path.join(WORK_DIR, "pipeline_smart.log")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt") 
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

# Engine Suche
def find_qe_exec(tool_names):
    search_paths = ["/usr/bin", "/usr/local/bin", r"C:\Quantum_Espresso", os.path.expanduser("~")+"/bin"]
    for path in search_paths:
        for name in tool_names:
            full_path = os.path.join(path, name)
            if os.path.exists(full_path): return full_path
    return None

PW_EXE = find_qe_exec(["pw.x", "pw.exe"])
PH_EXE = find_qe_exec(["ph.x", "ph.exe"])
DOS_EXE = find_qe_exec(["dos.x", "dos.exe"])

if not PW_EXE or not PH_EXE or not DOS_EXE:
    print("❌ FEHLER: Quantum Espresso Programme nicht gefunden!")
    sys.exit()

# =============================================================================
# 2. HELFER & NOTIFICATION & CSV
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC Update: {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def git_sync(message):
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR)
        subprocess.run(["git", "push"], cwd=WORK_DIR)
    except: pass

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-"):
    """Aktualisiert die CSV-Datei mit ALLEN Feldern."""
    file_exists = os.path.isfile(CSV_FILE)
    rows = []
    updated = False
    
    # Hier müssen ALLE Spalten stehen, die jemals vorkommen könnten
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Timestamp']

    if file_exists:
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['Name'] == name:
                    row['Status'] = status
                    if e_fermi != "-": row['Fermi Energie (eV)'] = str(e_fermi)
                    if dos_val != "-": row['DOS @ Fermi'] = str(dos_val)
                    if is_metal != "-": row['Metall?'] = str(is_metal)
                    if min_f != "-": row['Min Freq (THz)'] = str(min_f)
                    if stab != "-": row['Stabilität'] = str(stab)
                    row['Timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    updated = True
                rows.append(row)
    
    if not updated:
        rows.append({
            'Name': name, 'Status': status, 'Fermi Energie (eV)': str(e_fermi),
            'DOS @ Fermi': str(dos_val), 'Metall?': str(is_metal),
            'Min Freq (THz)': str(min_f), 'Stabilität': str(stab),
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")
        })
        
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def disable_logic_app():
    print(f"🛑 Deaktiviere Logic App: {LOGIC_APP_NAME}...")
    try:
        cmd = ["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", "Disabled"]
        subprocess.run(cmd, capture_output=True)
    except: pass

def enable_logic_app():
    print(f"🟢 Aktiviere Logic App: {LOGIC_APP_NAME}...")
    try:
        cmd = ["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", "Enabled"]
        subprocess.run(cmd, capture_output=True)
    except: pass

def smart_shutdown(reason="Fertig"):
    print(f"\n🔌 Leite Shutdown ein: {reason}")
    disable_logic_app()
    try:
        with open(SIGNAL_FILE, "w") as f:
            f.write(f"Status: {reason}\nTimestamp: {time.ctime()}")
    except: pass
    
    # Shutdown
    try:
        if os.name != 'nt': os.system("sudo shutdown -h now")
    except: pass

def emergency_shutdown(error_msg):
    full_error = f"{error_msg}\n{traceback.format_exc()}"
    with open(LOG_FILE, "w") as f: f.write(full_error)
    send_notification(f"🚨 STOPP: {error_msg}")
    smart_shutdown(reason="Emergency Error")
    sys.exit()

# =============================================================================
# 3. PHYSIK-CHECKER
# =============================================================================
def get_fermi_energy(scf_out_path):
    if not os.path.exists(scf_out_path): return None
    try:
        with open(scf_out_path, 'r') as f: content = f.read()
        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", content)
        if match: return float(match.group(1))
    except: pass
    return None

def check_metallicity(dos_out_path, e_fermi):
    """Prüft, ob DOS am Fermi-Level hoch genug ist."""
    if not os.path.exists(dos_out_path) or e_fermi is None: return False, 0.0
    
    dos_at_fermi = 0.0
    closest_diff = 99.9
    
    try:
        with open(dos_out_path, 'r') as f:
            for line in f:
                if line.strip().startswith("#"): continue
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        e = float(parts[0])
                        d = float(parts[1])
                        # Finde den Wert am nächsten an E_Fermi
                        diff = abs(e - e_fermi)
                        if diff < closest_diff:
                            closest_diff = diff
                            dos_at_fermi = d
                    except: continue
        
        is_metal = dos_at_fermi > DOS_THRESHOLD
        return is_metal, dos_at_fermi
    except: return False, 0.0

# =============================================================================
# 4. PUPPET MASTER (Optimierung)
# =============================================================================
def get_prefix_from_content(content):
    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
    return match.group(1) if match else "calc"

def update_input_params(input_file, iteration_count):
    target_beta = 0.7
    if iteration_count >= 90: target_beta = 0.15
    elif iteration_count >= 60: target_beta = 0.25
    elif iteration_count >= 30: target_beta = 0.4
    else: return False

    with open(input_file, 'r') as f: content = f.read()
    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
        with open(input_file, 'w') as f: f.write(content)
        return True
    return False

def get_last_iteration(output_file):
    if not os.path.exists(output_file): return 0
    try:
        file_size = os.path.getsize(output_file)
        with open(output_file, 'rb') as f:
            f.seek(max(0, file_size - 10000), 0) 
            chunk = f.read().decode('utf-8', errors='ignore')
        matches = re.findall(r"iteration #\s*(\d+)", chunk)
        return int(matches[-1]) if matches else 0
    except: return 0

def run_monitored_pw(input_file, output_file, cwd):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" not in content:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")
    with open(input_file, 'w') as f: f.write(content)

    while True:
        with open(input_file, 'r') as f: content = f.read()
        mode = 'restart' if (os.path.exists(output_file)) else 'from_scratch'
        if "restart_mode" not in content:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        else:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
            
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            # 4 Kerne nutzen!
            cmd = ["mpirun", "--oversubscribe", "-np", "4", PW_EXE]
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, cwd=cwd)
            
            killed = False
            try:
                while process.poll() is None:
                    time.sleep(10)
                    cur_iter = get_last_iteration(output_file)
                    if update_input_params(input_file, cur_iter):
                        process.terminate()
                        killed = True
                        break
            except: process.kill(); return False
            
        if killed: continue 
        
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return True
        return False

# =============================================================================
# 5. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        enable_logic_app()
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        input_files = glob.glob(os.path.join(INPUTS_DIR, "*.in"))
        
        if not input_files:
            smart_shutdown("Leerlauf - Keine Inputs")
            sys.exit()

        send_notification(f"Start: {len(input_files)} Jobs in der Queue.")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            print(f"\n💎 Job: {name}")
            
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            if not os.path.exists(work_dir): os.makedirs(work_dir)
            
            scf_in = os.path.join(work_dir, "scf.in")
            scf_out = os.path.join(work_dir, "scf.out")
            dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
            ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

            if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)
            
            # --- 1. SCF ---
            update_csv(name, "Rechnet SCF...")
            scf_done = False
            if os.path.exists(scf_out) and "JOB DONE" in open(scf_out).read(): scf_done = True
            
            if not scf_done:
                print("   1️⃣  Starte SCF...")
                if not run_monitored_pw(scf_in, scf_out, work_dir):
                    print("   ❌ SCF fehlgeschlagen.")
                    update_csv(name, "Fehler (SCF)")
                    send_notification(f"⚠️ {name}: SCF fehlgeschlagen.")
                    continue
            
            with open(scf_in, 'r') as f: scf_content = f.read()
            prefix = get_prefix_from_content(scf_content)
            e_fermi = get_fermi_energy(scf_out)

            # --- 2. DOS ---
            update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
            if not os.path.exists(dos_out):
                print("   2️⃣  DOS Berechnung...")
                dos_content = f"""&DOS
  prefix='{prefix}',
  outdir='./tmp',
  fildos='{name}.dos',
  Emin=-20.0, Emax=30.0, DeltaE=0.1
/
"""
                with open(dos_in, "w") as f: f.write(dos_content)
                with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                    subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, cwd=work_dir)

            # --- 3. METALL CHECK ---
            is_metal, dos_val = check_metallicity(dos_out, e_fermi)
            
            if not is_metal:
                print(f"   🛑 {name} ist ein Isolator (DOS={dos_val:.3f}). Phononen übersprungen.")
                update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                # KORRIGIERT: Telegram Nachricht pro Isolator
                send_notification(f"🛑 {name} fertig: Isolator (Skip Phonons).")
                git_sync(f"Fertig: {name} (Isolator)")
                continue # Nächster Kandidat in der Schleife

            # --- 4. PHONONEN (Nur wenn Metall) ---
            print(f"   ⚡ {name} ist ein Metall (DOS={dos_val:.3f}). Berechne Phononen...")
            update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")
            
            min_f = "-"
            stab = "Unbekannt"
            
            if not os.path.exists(ph_out):
                print("   3️⃣  Phononen Berechnung...")
                ph_content = f"""Phonons for {name}
&INPUTPH
  tr2_ph=1.0d-14,
  prefix='{prefix}',
  outdir='./tmp',
  fildyn='{name}.dyn',
  ldisp=.true.,
  nq1=2, nq2=2, nq3=2
/
"""
                with open(ph_in, "w") as f: f.write(ph_content)
                with open(ph_in, "r") as f_in, open(ph_out, "w") as f_out:
                    cmd_ph = ["mpirun", "--oversubscribe", "-np", "4", PH_EXE] # 4 Kerne!
                    subprocess.run(cmd_ph, stdin=f_in, stdout=f_out, cwd=work_dir)

            # Frequenzen checken, falls PH fertig ist
            if os.path.exists(ph_out):
                 with open(ph_out, 'r') as f:
                     content = f.read()
                     if "JOB DONE" in content:
                         freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
                         if freqs:
                             freqs = [float(f) for f in freqs]
                             min_f = min(freqs)
                             stab = "STABIL" if min_f > -0.05 else "INSTABIL"

            update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
            # KORRIGIERT: Telegram Nachricht pro Metall
            send_notification(f"✅ {name} fertig: Metall & Phononen berechnet.")
            git_sync(f"Fertig: {name} (Metall)")

        send_notification("🎉 Alle Jobs der Liste abgearbeitet.")
        smart_shutdown("Pipeline Success - Alles fertig")

    except Exception as e: emergency_shutdown(f"Error: {e}")

if __name__ == "__main__":
    main()