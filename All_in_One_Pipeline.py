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
TELEGRAM_TOKEN = "8202414068:AAHnnLMa7nfo0E3gCDLUVnUmIomoyveDPBA"
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
# 3. PUPPET MASTER (SMART LOGIC)
# =============================================================================

def analyze_crash_reason(output_file):
    """
    Entscheidet zwischen DONE, HARD (Error), SOFT (Timeout) und NON_CONVERGED.
    """
    if not os.path.exists(output_file): return "NONE"
    
    try:
        # Lese die letzten 100 Zeilen
        with open(output_file, 'rb') as f:
            try:
                f.seek(-10000, 2) # Gehe ans Ende
            except OSError:
                f.seek(0) # Datei ist zu klein, lese alles
            lines = f.read().decode('utf-8', errors='ignore')
            
        # 1. Erfolgreich?
        if "JOB DONE" in lines:
            return "DONE"
            
        # 2. NEU: Konvergenz nicht erreicht (Iter > 150)
        if "convergence NOT achieved" in lines:
            return "NON_CONVERGED"
            
        # 3. Hard Crash Keywords
        error_keywords = ["Error", "error", "Mpi_Abort", "segmentation fault", "stopping", "diagonalization failed"]
        for key in error_keywords:
            if key in lines:
                return "HARD"
        
        # 4. Soft Crash (Einfach aufgehört -> Timeout)
        return "SOFT"
    except:
        return "HARD" # Im Zweifel neu machen

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    
    # --- A. Standard Pfade fixen ---
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    # --- B. Dynamische Parameter (Smart Logic) ---
    
    # 1. Mixing Beta (gegen Oszillationen)
    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    
    # 2. Convergence Threshold Lockern
    if iteration_count >= 60:
        new_conv = "1.0d-5"
        if "conv_thr" in content:
             content = re.sub(r"conv_thr\s*=\s*[0-9\.dD\-]+", f"conv_thr = {new_conv}", content)
        else:
             content = content.replace("&ELECTRONS", f"&ELECTRONS\n conv_thr = {new_conv},")
             
    # 3. Geometrie Optimierung lockern
    if iteration_count >= 100:
        new_etot = "1.0d-3"
        new_forc = "1.0d-2"
        
        if "etot_conv_thr" in content:
            content = re.sub(r"etot_conv_thr\s*=\s*[0-9\.dD\-]+", f"etot_conv_thr = {new_etot}", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n etot_conv_thr = {new_etot},")
            
        if "forc_conv_thr" in content:
            content = re.sub(r"forc_conv_thr\s*=\s*[0-9\.dD\-]+", f"forc_conv_thr = {new_forc}", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n forc_conv_thr = {new_forc},")
    
    # --- C. RAM Optimierung & LIMITS ---
    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", "diagonalization='cg'", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n diagonalization='cg',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", "mixing_ndim = 4", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_ndim = 4,")

    # NEU: Limit auf 150 setzen
    if "electron_maxstep" in content:
        content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 150", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 150,")

    with open(input_file, 'w') as f: f.write(content)
    return True

def get_last_iteration(output_file):
    if not os.path.exists(output_file): return 0
    try:
        file_size = os.path.getsize(output_file)
        with open(output_file, 'rb') as f:
            f.seek(max(0, file_size - 10000), 0) 
            chunk = f.read().decode('utf-8', errors='ignore')
        # Zähle SCF iterationen ODER BFGS steps
        bfgs_matches = re.findall(r"number of bfgs steps\s*=\s*(\d+)", chunk)
        scf_matches = re.findall(r"iteration #\s*(\d+)", chunk)
        
        val = 0
        if bfgs_matches: val = int(bfgs_matches[-1])
        elif scf_matches: val = int(scf_matches[-1])
        return val
    except: return 0

def run_monitored_pw(input_file, output_file, cwd):
    # Initial Setup (Iter 0)
    fix_input_file(input_file, 0)
    
    while True:
        with open(input_file, 'r') as f: content = f.read()
        tmp_dir = os.path.join(cwd, "tmp")
        
        # Check ob wir Resumen können
        can_restart = os.path.exists(output_file) and os.path.exists(tmp_dir)
        mode = 'restart' if can_restart else 'from_scratch'
        
        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", NUM_CORES, PW_EXE]
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
            
            try:
                while process.poll() is None:
                    time.sleep(10)
                    # Live Überwachung für Parameter-Anpassung
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
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        set_logic_app_state("Enabled")
        
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART DER PIPELINE: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        print(f"\n\n{'='*40}\n🚀 NEUSTART DER PIPELINE: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        # --- PIPELINE START ---
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start: {len(input_files)} Jobs in der Queue.")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out = os.path.join(work_dir, "scf.out")
            
            last_status = get_csv_status(name)
            
            if "Fertig" in last_status or "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status: {last_status})")
                continue

            # --- SMART CRASH HANDLING ---
            crash_type = analyze_crash_reason(scf_out)
            
            if crash_type == "HARD":
                # --- ÄNDERUNG HIER: Ordner behalten & überspringen ---
                print(f"❌ {name}: HARD CRASH erkannt. Überspringe Job, um Logs zu sichern.")
                update_csv(name, "SKIPPED (Hard Crash)")
                git_sync(f"Hard Crash preserved: {name}")
                continue
            
            elif crash_type == "NON_CONVERGED":
                print(f"⏩ {name} konvergiert nicht (Max Steps erreicht). Wird übersprungen.")
                update_csv(name, "SKIPPED (Non-Conv)")
                git_sync(f"Skipped: {name} (Non-Conv)")
                continue

            elif crash_type == "SOFT":
                print(f"♻️  Retry: {name} (SOFT CRASH/TIMEOUT -> Resume ohne Löschen)")
            
            elif crash_type == "DONE":
                print(f"✅ {name} scheint fertig zu sein (laut Log). Update Status.")
            
            # --- JOB STARTEN ---
            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                
                print(f"\n💎 Job: {name}")
                git_sync(f"Start Job: {name}") 

                scf_in = os.path.join(work_dir, "scf.in")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                # --- 1. SCF ---
                update_csv(name, "Rechnet SCF...") 
                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out).read()):
                    print("   1️⃣  Starte SCF (Monitored)...")
                    success = run_monitored_pw(scf_in, scf_out, work_dir)
                    
                    if not success:
                        # Nach Fehlschlag: Prüfen ob es an Konvergenz lag
                        fail_reason = analyze_crash_reason(scf_out)
                        if fail_reason == "NON_CONVERGED":
                             print(f"   ❌ SCF nicht konvergiert (Limit 150). Skip.")
                             update_csv(name, "SKIPPED (Non-Conv)")
                             git_sync(f"Skipped: {name} (Non-Conv)")
                        else:
                             print(f"   ❌ SCF fehlgeschlagen (Crash)!")
                             update_csv(name, "CRASHED (SCF)")
                             git_sync(f"SCF Fehler: {name}")
                        continue 

                # Daten extrahieren
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