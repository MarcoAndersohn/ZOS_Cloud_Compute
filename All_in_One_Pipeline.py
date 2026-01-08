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

# Deine Azure Logic App (Wächter)
LOGIC_APP_NAME = "Wächter" 
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"

# Schwellenwert: Alles unter 0.05 Zuständen/eV am Fermi-Level gilt als Isolator
DOS_THRESHOLD = 0.05 

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
CSV_FILE = os.path.join(WORK_DIR, "Global_Status_Report.csv")
LOG_FILE = os.path.join(WORK_DIR, "pipeline_smart.log")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt") 

# Engine Suche
def find_qe_exec(tool_names):
    search_paths = ["/usr/bin", "/usr/local/bin", os.path.expanduser("~")+"/bin"]
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
# 2. HELFER: CSV & NOTIFICATION
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🤖 HPC Update: {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-"):
    """Aktualisiert die CSV-Datei sofort."""
    file_exists = os.path.isfile(CSV_FILE)
    
    # Daten lesen, falls vorhanden, um Duplikate zu vermeiden (einfaches Append hier)
    # Für eine robuste Lösung lesen wir alles ein und überschreiben die Zeile des aktuellen Jobs
    rows = []
    updated = False
    
    if file_exists:
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['Name'] == name:
                    # Update existing row
                    row['Status'] = status
                    row['Fermi_eV'] = str(e_fermi)
                    row['DOS_at_Fermi'] = str(dos_val)
                    row['Is_Metal'] = str(is_metal)
                    row['Last_Update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    updated = True
                rows.append(row)
    
    if not updated:
        rows.append({
            'Name': name,
            'Status': status,
            'Fermi_eV': str(e_fermi),
            'DOS_at_Fermi': str(dos_val),
            'Is_Metal': str(is_metal),
            'Last_Update': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        
    fieldnames = ['Name', 'Status', 'Fermi_eV', 'DOS_at_Fermi', 'Is_Metal', 'Last_Update']
    
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def git_sync(message):
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR)
        subprocess.run(["git", "push"], cwd=WORK_DIR)
    except: pass

# =============================================================================
# 3. AZURE STEUERUNG
# =============================================================================
def disable_logic_app():
    print(f"🛑 Deaktiviere Logic App: {LOGIC_APP_NAME}...")
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", "Disabled"], capture_output=True)
    except: pass

def enable_logic_app():
    print(f"🟢 Aktiviere Logic App: {LOGIC_APP_NAME}...")
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", "Enabled"], capture_output=True)
    except: pass

def smart_shutdown(reason="Fertig"):
    print(f"\n🔌 Leite Shutdown ein: {reason}")
    disable_logic_app()
    try:
        # Azure Metadata Service für Shutdown
        requests.post("http://169.254.169.254/metadata/instance/compute/scheduleEvents?api-version=2020-09-01") # Dummy call
        os.system("sudo shutdown -h now") 
    except: pass

# =============================================================================
# 4. PHYSIK-LOGIK (PUPPET MASTER & ANALYSE)
# =============================================================================
def get_fermi_from_scf(scf_out_path):
    if not os.path.exists(scf_out_path): return None
    try:
        with open(scf_out_path, 'r') as f: content = f.read()
        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", content)
        return float(match.group(1)) if match else None
    except: return None

def analyze_metallicity(dos_out_path, e_fermi):
    if not os.path.exists(dos_out_path) or e_fermi is None: return False, 0.0
    
    dos_at_fermi = 0.0
    closest_diff = 99.9
    
    try:
        with open(dos_out_path, 'r') as f:
            for line in f:
                if line.strip().startswith("#"): continue
                parts = line.split()
                if len(parts) >= 2:
                    e = float(parts[0])
                    d = float(parts[1])
                    diff = abs(e - e_fermi)
                    if diff < closest_diff:
                        closest_diff = diff
                        dos_at_fermi = d
        
        # Entscheidung
        is_metal = dos_at_fermi > DOS_THRESHOLD
        return is_metal, dos_at_fermi
    except: return False, 0.0

def update_input_params(input_file, iteration_count):
    # Deine bewährte Puppet-Master Logik zur Konvergenz-Rettung
    target_beta = 0.7
    if iteration_count >= 60: target_beta = 0.2
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
        # Liest nur die letzten Bytes für Speed
        with open(output_file, 'rb') as f:
            try: f.seek(-5000, 2) 
            except: pass
            chunk = f.read().decode('utf-8', errors='ignore')
        matches = re.findall(r"iteration #\s*(\d+)", chunk)
        return int(matches[-1]) if matches else 0
    except: return 0

def run_monitored_pw(input_file, output_file, cwd):
    # Stellt sicher, dass restart_mode korrekt ist und mixed beta
    with open(input_file, 'r') as f: content = f.read()
    mode = 'restart' if (os.path.exists(output_file)) else 'from_scratch'
    
    # Pseudo Dir fixen
    corr_pseudo = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" not in content:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_pseudo}',")
    
    # Restart Mode setzen
    if "restart_mode" not in content:
        content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
    else:
        content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
    
    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)
    
    file_mode = 'a' if mode == 'restart' else 'w'

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        # 4 Kerne Optimierung
        cmd = ["mpirun", "--oversubscribe", "-np", "4", PW_EXE]
        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, cwd=cwd)
        
        # Überwachungsschleife
        killed = False
        while process.poll() is None:
            time.sleep(10)
            cur_iter = get_last_iteration(output_file)
            if update_input_params(input_file, cur_iter):
                process.terminate()
                killed = True
                break
        
        if killed: return run_monitored_pw(input_file, output_file, cwd) # Rekursiver Neustart
        
    # Check Erfolg
    if os.path.exists(output_file):
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return True
    return False

# =============================================================================
# 5. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        enable_logic_app()
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        input_files = glob.glob(os.path.join(INPUTS_DIR, "*.in"))
        input_files.sort()

        if not input_files:
            smart_shutdown("Keine Inputs")
            sys.exit()

        send_notification(f"🚀 Start Smart-Pipeline: {len(input_files)} Kandidaten.")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            print(f"\n💎 Bearbeite: {name}")
            
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            if not os.path.exists(work_dir): os.makedirs(work_dir)
            
            # Dateipfade
            scf_in = os.path.join(work_dir, "scf.in")
            scf_out = os.path.join(work_dir, "scf.out")
            dos_in = os.path.join(work_dir, "dos.in")
            dos_out = os.path.join(work_dir, f"{name}.dos")
            ph_in = os.path.join(work_dir, "ph.in")
            ph_out = os.path.join(work_dir, "ph.out")

            if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)
            
            # 1. SCF
            update_csv(name, "SCF läuft...")
            scf_done = False
            if os.path.exists(scf_out) and "JOB DONE" in open(scf_out).read(): scf_done = True
            
            if not scf_done:
                print("   1️⃣  Starte SCF...")
                if not run_monitored_pw(scf_in, scf_out, work_dir):
                    print("   ❌ SCF fehlgeschlagen.")
                    update_csv(name, "SCF ERROR")
                    send_notification(f"⚠️ {name}: SCF fehlgeschlagen.")
                    continue

            # Prefix extrahieren
            with open(scf_in, 'r') as f: 
                prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                prefix = prefix_match.group(1) if prefix_match else "calc"
            
            # Fermi Energie holen
            e_fermi = get_fermi_from_scf(scf_out)
            if e_fermi is None:
                print("   ❌ Keine Fermi-Energie gefunden.")
                continue

            # 2. DOS
            update_csv(name, "DOS läuft...", e_fermi=e_fermi)
            if not os.path.exists(dos_out):
                print("   2️⃣  DOS Berechnung...")
                dos_content = f"""&DOS
  prefix='{prefix}',
  outdir='./tmp',
  fildos='{name}.dos',
  Emin=-20.0, Emax=30.0, DeltaE=0.05
/
"""
                with open(dos_in, "w") as f: f.write(dos_content)
                with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                    subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, cwd=work_dir)

            # 3. METALL CHECK (DIE ENTSCHEIDUNG)
            is_metal, dos_val = analyze_metallicity(dos_out, e_fermi)
            metal_status = "JA" if is_metal else "NEIN"
            print(f"   📊 Check: Metall? {metal_status} (DOS@Ef = {dos_val:.4f})")

            if not is_metal:
                print("   🛑 Isolator erkannt. Überspringe Phononen.")
                update_csv(name, "Fertig (Isolator)", e_fermi, dos_val, "NEIN")
                send_notification(f"🛑 {name} ist ein Isolator. Phononen übersprungen.")
                git_sync(f"Fertig: {name} (Isolator)")
                continue # Nächster Kandidat

            # 4. PHONONEN (Nur wenn Metall)
            update_csv(name, "PHONONEN laufen...", e_fermi, dos_val, "JA")
            if not os.path.exists(ph_out):
                print("   3️⃣  Phononen Berechnung (Metall)...")
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
                    # Phononen brauchen viel Zeit -> Over subscribe auch hier
                    subprocess.run(["mpirun", "--oversubscribe", "-np", "4", PH_EXE], stdin=f_in, stdout=f_out, cwd=work_dir)
            
            update_csv(name, "Fertig (Metall + PH)", e_fermi, dos_val, "JA")
            send_notification(f"✅ {name} fertig (Metall). Phononen berechnet.")
            git_sync(f"Fertig: {name} (Metall)")

        # Ende
        send_notification("🎉 Pipeline vollständig abgeschlossen.")
        smart_shutdown("Alles erledigt")

    except Exception as e:
        err_msg = f"CRITICAL ERROR: {str(e)}\n{traceback.format_exc()}"
        print(err_msg)
        with open(os.path.join(WORK_DIR, "CRASH.log"), "w") as f: f.write(err_msg)
        send_notification("🚨 CRITICAL ERROR - Pipeline abgestürzt!")
        
if __name__ == "__main__":
    main()