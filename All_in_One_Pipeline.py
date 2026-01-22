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
import psutil
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

DEFAULT_CORES = "2"
SAFE_CORES = "1"
MEMORY_LIMIT_PERCENT = 88.0

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")

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
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
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
# 3. SMART LOGIC & VALIDATION
# =============================================================================

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try: f.seek(-10000, 2) 
            except OSError: f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore')
        if "JOB DONE" in lines: return "DONE"
        if "convergence NOT achieved" in lines: return "NON_CONVERGED"
        error_keywords = ["Error", "error", "Mpi_Abort", "segmentation fault", "stopping", "diagonalization failed", "fatal error reading xml"]
        for key in error_keywords:
            if key in lines: return "HARD"
        return "SOFT"
    except: return "HARD"

def is_xml_valid(xml_path):
    """
    Prüft intelligent, ob die XML-Datei vollständig geschrieben wurde.
    Wir schauen uns nur die letzten Bytes an, um das End-Tag zu finden.
    """
    if not os.path.exists(xml_path): return False
    try:
        with open(xml_path, 'rb') as f:
            try: f.seek(-1000, 2) # Lese nur die letzten 1000 Bytes
            except: f.seek(0)
            tail = f.read().decode('utf-8', errors='ignore')
        
        # Das sind die Zeichen, dass QE fertig mit Schreiben war:
        if "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail:
            return True
        return False
    except:
        return False

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    # Dynamische Anpassung der Konvergenzparameter bei Problemen
    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    
    if iteration_count >= 60:
        new_conv = "1.0d-5"
        if "conv_thr" in content:
             content = re.sub(r"conv_thr\s*=\s*[0-9\.dD\-]+", f"conv_thr = {new_conv}", content)
        else:
             content = content.replace("&ELECTRONS", f"&ELECTRONS\n conv_thr = {new_conv},")
             
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
    
    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", "diagonalization='cg'", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n diagonalization='cg',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", "mixing_ndim = 4", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_ndim = 4,")

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
        bfgs_matches = re.findall(r"number of bfgs steps\s*=\s*(\d+)", chunk)
        scf_matches = re.findall(r"iteration #\s*(\d+)", chunk)
        val = 0
        if bfgs_matches: val = int(bfgs_matches[-1])
        elif scf_matches: val = int(scf_matches[-1])
        return val
    except: return 0

# --- INTELLIGENTE LAUF-FUNKTION ---
def run_monitored_pw(input_file, output_file, cwd, active_cores):
    fix_input_file(input_file, 0)
    
    last_git_sync = time.time()
    last_checkpoint_time = 0 # Timer für lokale Checkpoints

    while True:
        with open(input_file, 'r') as f: content = f.read()
        tmp_dir = os.path.join(cwd, "tmp") 
        checkpoint_dir = os.path.join(cwd, "tmp_SAFE_CHECKPOINT") # Unser Sicherheits-Ordner

        # Pfade ermitteln
        prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
        current_prefix = prefix_match.group(1) if prefix_match else "calc"
        xml_path = os.path.join(tmp_dir, f"{current_prefix}.save", "data-file-schema.xml")
        
        # --- INTELLIGENTER START-CHECK ---
        mode = 'from_scratch'
        
        # 1. Ist der aktuelle tmp-Ordner heil?
        if os.path.exists(output_file) and is_xml_valid(xml_path):
            mode = 'restart'
            print("      ✅ Gültige XML im tmp-Ordner gefunden -> Normaler Restart.")
            
        # 2. Ist tmp kaputt, aber wir haben einen Safe-Checkpoint?
        elif os.path.exists(output_file) and os.path.exists(checkpoint_dir):
            print("      🛡️ tmp-Ordner defekt/unvollständig! Hole Safe-Checkpoint...")
            try:
                # Wir löschen den kaputten tmp Ordner
                if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
                # Wir kopieren den heilen Checkpoint zurück
                shutil.copytree(checkpoint_dir, tmp_dir)
                
                # Kurzer Check ob das geklappt hat
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint erfolgreich geladen! Mache weiter wo wir sicher waren.")
                else:
                    print("      ❌ Checkpoint war auch defekt. Starte von vorne.")
            except Exception as e:
                print(f"      ❌ Fehler beim Laden des Checkpoints: {e}")
        
        else:
            print("      🆕 Kein gültiger Speicherstand gefunden -> Starte von vorne (From Scratch).")

        # Aufräumen wenn wir eh von vorne anfangen
        if mode == 'from_scratch':
            if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir, ignore_errors=True)
            # Checkpoint lassen wir leben, vielleicht brauchen wir ihn später noch? 
            # Nein, bei from_scratch ist er veraltet.
            if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir, ignore_errors=True)

        # Input File anpassen
        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE]
            print(f"      ⚙️ Starte PWSCF ({mode})...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
            
            try:
                while process.poll() is None:
                    time.sleep(5) # Nicht zu oft pollen
                    
                    # A. GIT SYNC (Logs, nicht Daten!) - Alle 30 Min
                    if time.time() - last_git_sync > 1800:
                        print("      ☁️ Sync Logs to Git...")
                        git_sync("Log Update")
                        last_git_sync = time.time()

                    # B. INTELLIGENTER CHECKPOINT (Alle 15 Min prüfen)
                    if time.time() - last_checkpoint_time > 900: # 15 min
                        # Wir kopieren NUR, wenn die XML gerade heil ist!
                        if is_xml_valid(xml_path):
                            print("      💾 XML ist valide -> Erstelle Sicherheits-Kopie (Checkpoint)...")
                            try:
                                # Alte Kopie weg (wir brauchen nur den letzten heilen Stand)
                                if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir)
                                # Neue Kopie hin (das dauert kurz, ist aber sicher)
                                shutil.copytree(tmp_dir, checkpoint_dir)
                                last_checkpoint_time = time.time()
                                print("      ✅ Checkpoint erstellt.")
                            except Exception as e:
                                print(f"      ⚠️ Checkpoint fehlgeschlagen: {e}")
                        # Wenn XML nicht valide (weil QE gerade schreibt), machen wir nix und probieren es beim nächsten Loop

                    # C. RAM CHECK
                    mem_usage = psutil.virtual_memory().percent
                    if mem_usage > MEMORY_LIMIT_PERCENT:
                        print(f"      ⚠️ RAM NOT-AUS!")
                        process.kill()
                        return "OOM" 

                    # D. Input Tweaking bei Problemen
                    cur_iter = get_last_iteration(output_file)
                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except: 
                process.kill(); return "CRASH"
            
        if process.returncode != 0: return "CRASH"
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return "DONE"
            return "CRASH"

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        set_logic_app_state("Enabled")
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start: {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out = os.path.join(work_dir, "scf.out")
            
            last_status = get_csv_status(name)
            if "Fertig" in last_status or "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name}")
                continue

            # Crash Check vorab
            crash_type = analyze_crash_reason(scf_out)
            if crash_type == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                continue
            elif crash_type == "DONE":
                print(f"✅ {name} ist fertig.")
            
            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                print(f"\n💎 Job: {name}")
                scf_in = os.path.join(work_dir, "scf.in")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                # --- SCF LOOP ---
                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read()):
                    update_csv(name, "Rechnet SCF...")
                    current_cores = int(DEFAULT_CORES)
                    
                    while True:
                        print(f"   1️⃣  SCF ({current_cores} Cores)")
                        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)
                        
                        if result == "DONE": break 
                        elif result == "OOM":
                            if current_cores > 1:
                                current_cores = int(SAFE_CORES)
                                update_csv(name, "SCF (Low Mem)")
                                continue 
                            else:
                                update_csv(name, "FAILED (OOM)")
                                break
                        elif result == "CRASH":
                            reason = analyze_crash_reason(scf_out)
                            if reason == "NON_CONVERGED":
                                update_csv(name, "SKIPPED (Non-Conv)")
                                break
                            else:
                                update_csv(name, "Retrying (Crash)")
                                time.sleep(2)
                                continue 

                    if analyze_crash_reason(scf_out) != "DONE":
                        git_sync(f"Failed: {name}")
                        continue 

                # Daten extrahieren
                with open(scf_in, 'r') as f: 
                    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = match.group(1) if match else "calc"
                
                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r', errors='ignore') as f:
                        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", f.read())
                        if match: e_fermi = float(match.group(1))

                # --- DOS ---
                update_csv(name, "Rechnet DOS...", e_fermi=e_fermi)
                if not os.path.exists(dos_out):
                    with open(dos_in, "w") as f: 
                        f.write(f"&DOS\n prefix='{prefix}', outdir='./tmp', fildos='{name}.dos', Emin=-20.0, Emax=30.0, DeltaE=0.1 /\n")
                    with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                        subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)

                is_metal, dos_val = False, 0.0
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
                    print(f"   🛑 Isolator (DOS={dos_val:.3f}).")
                    update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                    git_sync(f"Fertig: {name} (Isolator)")
                    continue

                # --- PHONONEN ---
                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")
                if not os.path.exists(ph_out):
                    with open(ph_in, "w") as f: 
                        f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., nq1=2, nq2=2, nq3=2 /\n")
                    with open(ph_in, "r") as f_in, open(ph_out, "w") as f_out:
                        subprocess.run(["mpirun", "--oversubscribe", "-np", DEFAULT_CORES, PH_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)

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
                git_sync(f"Fertig: {name} (Metall)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}: {job_err}")
                update_csv(name, f"ERROR (Python: {str(job_err)[:30]})")
                continue 

        send_notification("🎉 Alle Jobs erledigt.")
        set_logic_app_state("Disabled") 
        with open(SIGNAL_FILE, "w") as f: f.write(f"Status: Fertig\nTimestamp: {time.ctime()}")
        if os.name != 'nt': os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()}):\n{e}\n{traceback.format_exc()}\n"
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        send_notification(f"🚨 KRITISCHER FEHLER: {e} -> Shutdown.")
        set_logic_app_state("Disabled")
        if os.name != 'nt': os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()