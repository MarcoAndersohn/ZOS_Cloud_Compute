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
# 0. LIVE-LOGGING
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# =============================================================================
# 1. KONFIGURATION
# =============================================================================
TELEGRAM_TOKEN = "8589716957:AAHAAU26UrnwOWgL4OytPpmj0dSPnyWNwu0"
TELEGRAM_CHAT_ID = "711461437"
LOGIC_APP_NAME = "AutoRestart-Supraleiter"
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"
DOS_THRESHOLD = 0.05

# VM Konfiguration (2 Kerne)
NUM_CORES = "2"

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")
SMART_LOG_FILE = os.path.join(WORK_DIR, "pipeline_smart.log")

PW_EXE = shutil.which("pw.x") or "/usr/bin/pw.x"
PH_EXE = shutil.which("ph.x") or "/usr/bin/ph.x"
DOS_EXE = shutil.which("dos.x") or "/usr/bin/dos.x"

# =============================================================================
# 2. HELFER & GIT
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC: {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def set_logic_app_state(state="Enabled"):
    """
    Schaltet die Logic App an oder aus.
    Wichtig: 'Disabled' verhindert den automatischen Neustart der VM.
    """
    if not shutil.which("az"): return
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except: pass

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, env=env, timeout=30)
        # Pull mit 'ours' Strategie -> HPC gewinnt immer bei Konflikten
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
    except subprocess.TimeoutExpired:
        print("⚠️ Git-Sync Timeout. Mache weiter...")
    except Exception as e:
        print(f"⚠️ Git Fehler: {e}")

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-"):
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Timestamp']
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f: rows = list(csv.DictReader(f))
    
    found = False
    for row in rows:
        if row['Name'] == name:
            row.update({'Status': status, 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")})
            if e_fermi != "-": row['Fermi Energie (eV)'] = str(e_fermi)
            if dos_val != "-": row['DOS @ Fermi'] = str(dos_val)
            if is_metal != "-": row['Metall?'] = str(is_metal)
            if min_f != "-": row['Min Freq (THz)'] = str(min_f)
            if stab != "-": row['Stabilität'] = str(stab)
            found = True
            break
            
    if not found:
        rows.append({'Name': name, 'Status': status, 'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val), 'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f), 'Stabilität': str(stab), 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")})
    
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def get_csv_status(name):
    if not os.path.exists(CSV_FILE): return "NEW"
    with open(CSV_FILE, 'r') as f:
        for row in csv.DictReader(f):
            if row['Name'] == name: return row['Status']
    return "NEW"

# =============================================================================
# 3. PUPPET MASTER (CORE LOGIC)
# =============================================================================
def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    
    # 1. Pseudo Directory fixen
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    # 2. Mixing Beta dynamisch anpassen
    target_beta = 0.7
    if iteration_count >= 90: target_beta = 0.15
    elif iteration_count >= 60: target_beta = 0.25
    elif iteration_count >= 30: target_beta = 0.4

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    
    # 3. RAM-OPTIMIERUNG (FIX: mixing_ndim auf 6 für Balance)
    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", "mixing_ndim = 6", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_ndim = 6,")

    # 3b. diago_david_ndim auf 2 setzen (Diagonalisierungs-RAM sparen)
    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")

    # 4. Max Steps
    if "electron_maxstep" not in content:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 300,")

    with open(input_file, 'w') as f: f.write(content)
    return True

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
    fix_input_file(input_file, 0)
    while True:
        with open(input_file, 'r') as f: content = f.read()
        tmp_dir = os.path.join(cwd, "tmp")
        can_restart = os.path.exists(output_file) and os.path.exists(tmp_dir) and os.listdir(tmp_dir)
        mode = 'restart' if can_restart else 'from_scratch'
        
        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            # NUM_CORES wird hier verwendet
            cmd = ["mpirun", "--oversubscribe", "-np", NUM_CORES, PW_EXE]
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
            
            try:
                while process.poll() is None:
                    time.sleep(10)
                    cur_iter = get_last_iteration(output_file)
                    if cur_iter > 30:
                         fix_input_file(input_file, cur_iter)
            except: 
                process.kill(); return False
            
        if process.returncode != 0: return False
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return True
            return False

# =============================================================================
# 4. HAUPTPROGRAMM (CRASH-RETRY MODUS)
# =============================================================================
def main():
    try:
        # CLEANUP & SETUP
        # HIER GEÄNDERT: Wir löschen die Logs NICHT mehr, damit wir Historie haben!
        # if os.path.exists(TXT_LOG_FILE):
        #    open(TXT_LOG_FILE, 'w').close()
        # if os.path.exists(SMART_LOG_FILE):
        #    open(SMART_LOG_FILE, 'w').close()
            
        set_logic_app_state("Enabled")
        
        # HIER GEÄNDERT: "a" Modus zum Anhängen
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART DER PIPELINE: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        print(f"\n\n{'='*40}\n🚀 NEUSTART DER PIPELINE: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        # --- 1. LEICHENSCHAU: Jobs finden, die das letzte Skript getötet haben ---
        if os.path.exists(CSV_FILE):
            rows = []
            with open(CSV_FILE, 'r') as f: rows = list(csv.DictReader(f))
            dirty = False
            for row in rows:
                if "Rechnet" in row['Status']:
                    killer_name = row['Name']
                    print(f"💀 Gefunden: {killer_name} hat den letzten Run abstürzen lassen. Wird markiert.")
                    # Wir markieren es, damit wir wissen, was passiert ist, aber der Loop unten
                    # wird es trotzdem retryen (wegen der Änderungen in Schritt 2).
                    row['Status'] = "CRASHED (System Absturz)"
                    row['Timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    dirty = True
            
            if dirty:
                with open(CSV_FILE, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Timestamp'])
                    writer.writeheader()
                    writer.writerows(rows)
                git_sync("Cleanup: Crashed Jobs geflaggt")

        # --- 2. NORMALE PIPELINE ---
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start: {len(input_files)} Jobs in der Queue.")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            
            last_status = get_csv_status(name)
            
            # KORREKTUR: CRASHED wird NICHT MEHR übersprungen, sondern neu gerechnet!
            if "Fertig" in last_status or "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status: {last_status})")
                continue

            # --- CRASH CLEANUP ---
            # Wenn der Job vorher gecrasht ist, müssen wir die alten Daten löschen,
            # sonst stürzt QE beim Versuch, die korrupten Dateien zu lesen, sofort wieder ab.
            if "CRASHED" in last_status:
                print(f"♻️  Retry: Lösche alten Ordner für {name} um sauber zu starten...")
                if os.path.exists(work_dir):
                    shutil.rmtree(work_dir) # Löscht den RUN-Ordner komplett

            # --- INNERER SCHUTZRING: Fängt Fehler pro Job ab ---
            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                
                print(f"\n💎 Job: {name}")
                git_sync(f"Start Job: {name}") 

                scf_in, scf_out = os.path.join(work_dir, "scf.in"), os.path.join(work_dir, "scf.out")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                # --- 1. SCF ---
                update_csv(name, "Rechnet SCF...") 
                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out).read()):
                    print("   1️⃣  Starte SCF...")
                    success = run_monitored_pw(scf_in, scf_out, work_dir)
                    
                    if not success:
                        print(f"   ❌ SCF fehlgeschlagen!")
                        update_csv(name, "Fehler (SCF)")
                        git_sync(f"SCF Fehler: {name}")
                        continue 

                with open(scf_in, 'r') as f: 
                    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = match.group(1) if match else "calc"
                
                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r') as f:
                        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", f.read())
                        if match: e_fermi = float(match.group(1))

                # --- 2. DOS ---
                update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
                if not os.path.exists(dos_out):
                    print("   2️⃣  DOS Berechnung...")
                    with open(dos_in, "w") as f: 
                        f.write(f"&DOS\n prefix='{prefix}', outdir='./tmp', fildos='{name}.dos', Emin=-20.0, Emax=30.0, DeltaE=0.1 /\n")
                    with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                        subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)

                # --- 3. METALL CHECK ---
                is_metal = False
                dos_val = 0.0
                if os.path.exists(dos_out) and e_fermi != "-":
                    closest_diff = 99.9
                    with open(dos_out, 'r') as f:
                        for line in f:
                            if line.strip().startswith("#"): continue
                            p = line.split()
                            if len(p) >= 2:
                                try:
                                    e, d = float(p[0]), float(p[1])
                                    if abs(e - e_fermi) < closest_diff:
                                        closest_diff = abs(e - e_fermi)
                                        dos_val = d
                                except: continue
                    is_metal = dos_val > DOS_THRESHOLD

                if not is_metal:
                    print(f"   🛑 {name} ist ein Isolator (DOS={dos_val:.3f}). Phononen übersprungen.")
                    update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                    git_sync(f"Fertig: {name} (Isolator)")
                    continue

                # --- 4. PHONONEN ---
                print(f"   ⚡ {name} ist ein Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")
                if not os.path.exists(ph_out):
                    print("   3️⃣  Phononen Berechnung...")
                    
                    with open(ph_in, "w") as f: 
                        f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., nq1=2, nq2=2, nq3=2 /\n")
                    with open(ph_in, "r") as f_in, open(ph_out, "w") as f_out:
                        subprocess.run(["mpirun", "--oversubscribe", "-np", NUM_CORES, PH_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)

                # --- 5. ENDE ---
                min_f, stab = "-", "Unbekannt"
                if os.path.exists(ph_out):
                      with open(ph_out, 'r') as f:
                          content = f.read()
                          if "JOB DONE" in content:
                              freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
                              if freqs:
                                  min_f = min([float(f) for f in freqs])
                                  stab = "STABIL" if min_f > -0.05 else "INSTABIL"

                update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                send_notification(f"✅ {name} fertig: Metall ({stab}).")
                git_sync(f"Fertig: {name} (Metall)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}: {job_err}")
                update_csv(name, f"ERROR (Python: {str(job_err)[:30]})")
                git_sync(f"Error caught: {name}")
                continue 

        # --- ENDE DES SKRIPTS (Normal) ---
        send_notification("🎉 Alle Jobs erledigt.")
        set_logic_app_state("Disabled") 
        with open(SIGNAL_FILE, "w") as f: f.write(f"Status: Fertig\nTimestamp: {time.ctime()}")
        if os.name != 'nt': os.system("sudo shutdown -h now")

    except Exception as e:
        # --- NOT-AUS (SYSTEM CRASH) ---
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()}):\n{e}\n{traceback.format_exc()}\n"
        
        # HIER GEÄNDERT: "a" für append (anhängen), damit wir nichts überschreiben
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        
        send_notification(f"🚨 KRITISCHER FEHLER: {e} -> Schalte Logic App & VM ab.")
        git_sync("Emergency Shutdown")
        print("🔕 Deaktiviere Logic App (Not-Aus)...")
        set_logic_app_state("Disabled")
        print("💤 Fahre VM herunter...")
        if os.name != 'nt': 
            os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()