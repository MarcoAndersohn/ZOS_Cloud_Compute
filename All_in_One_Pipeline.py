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
import math
from datetime import datetime

# =============================================================================
# 0. LIVE-LOGGING & CONFIG
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

try:
    TELEGRAM_TOKEN = open("/home/marco/.telegram_token").read().strip()
except:
    TELEGRAM_TOKEN = ""

TELEGRAM_CHAT_ID = "711461437"
DOS_THRESHOLD = 0.05
DEFAULT_CORES = "4"
SAFE_CORES = "2"
MEMORY_LIMIT_PERCENT = 85.0
MAX_BFGS_STEPS = 100 
MAX_RETRIES_LEVEL = 3

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")
TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")

PW_EXE = shutil.which("pw.x") or "/usr/bin/pw.x"
PH_EXE = shutil.which("ph.x") or "/usr/bin/ph.x"
DOS_EXE = shutil.which("dos.x") or "/usr/bin/dos.x"
Q2R_EXE = shutil.which("q2r.x") or "/usr/bin/q2r.x"
MATDYN_EXE = shutil.which("matdyn.x") or "/usr/bin/matdyn.x"

# =============================================================================
# 1. HELFER, GIT & CLEANUP
# =============================================================================
def send_notification(message):
    if not TELEGRAM_TOKEN:
        return
    try: 
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
            data={"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC {message}"}, 
            timeout=10
        )
    except: 
        pass

def ensure_gitignore():
    gi_path = os.path.join(WORK_DIR, ".gitignore")
    rules = [
        "*.wfc", "*.save/", "**/tmp/", "**/tmp_SAFE_*/", "**/tmp_PRISTINE*/", 
        "*.dvscf*", "*.a2Fsave*", "*.run", "*.dyn*", "*.recover*", "*.fc", "*.freq", "*.phdos"
    ]
    existing = []
    if os.path.exists(gi_path):
        with open(gi_path, 'r') as f:
            existing = f.read().splitlines()
    with open(gi_path, 'a') as f:
        for r in rules:
            if r not in existing:
                f.write(r + "\n")

def check_and_free_disk_space():
    try:
        free_gb = shutil.disk_usage("/").free / (1024**3)
        if free_gb < 5.0:
            print(f"      🧹 Festplatte voll ({free_gb:.2f} GB). Cleanup...")
            for f in glob.glob(os.path.join(WORK_DIR, "RUN_*", "tmp", "*.wfc*")):
                try:
                    os.remove(f)
                except:
                    pass
    except:
        pass

def initial_git_pull():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try: 
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
    except:
        pass

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        lock_file = os.path.join(WORK_DIR, ".git", "index.lock")
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except:
                pass
            
        subprocess.run(["git", "config", "credential.helper", "store"], cwd=WORK_DIR, env=env, timeout=10)
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, env=env, timeout=30)
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
    except:
        pass

def print_error_tail(log_file, lines=100):
    if not os.path.exists(log_file):
        return
    try:
        with open(log_file, 'r', errors='ignore') as f:
            tail = f.readlines()[-lines:]
        err = f"\n      --- LETZTE {lines} ZEILEN VON {os.path.basename(log_file)} ---\n" + "".join([f"      {l.rstrip()}\n" for l in tail])
        print(err)
        with open(TXT_LOG_FILE, 'a') as f_out:
            f_out.write(err)
    except:
        pass

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    try:
        lam = float(lambda_ep)
        wlog = float(omega_log_K)
        if lam <= 0 or (lam - mu_star * (1.0 + 0.62 * lam)) <= 0:
            return 0.0
        return (wlog / 1.20) * math.exp(-1.04 * (1.0 + lam) / (lam - mu_star * (1.0 + 0.62 * lam)))
    except:
        return "-"

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-", lam="-", wlog="-", tc="-"):
    rows = list(csv.DictReader(open(CSV_FILE, 'r'))) if os.path.exists(CSV_FILE) else []
    found = False
    for row in rows:
        if row['Name'] == name:
            row.update({'Status': status, 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")})
            if e_fermi != "-":
                row['Fermi Energie (eV)'] = str(e_fermi)
            if dos_val != "-":
                row['DOS @ Fermi'] = str(dos_val)
            if is_metal != "-":
                row['Metall?'] = str(is_metal)
            if min_f != "-":
                row['Min Freq (THz)'] = str(min_f)
            if stab != "-":
                row['Stabilität'] = str(stab)
            if lam != "-":
                row['Lambda'] = str(lam)
            if wlog != "-":
                row['Omega_log (K)'] = str(wlog)
            if tc != "-":
                row['Tc (K)'] = str(tc)
            found = True
            break
    if not found:
        rows.append({
            'Name': name, 
            'Status': status, 
            'Fermi Energie (eV)': str(e_fermi), 
            'DOS @ Fermi': str(dos_val), 
            'Metall?': str(is_metal), 
            'Min Freq (THz)': str(min_f), 
            'Stabilität': str(stab), 
            'Lambda': str(lam) if lam!="-" else "", 
            'Omega_log (K)': str(wlog) if wlog!="-" else "", 
            'Tc (K)': str(tc) if tc!="-" else "", 
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")
        })
    with open(CSV_FILE, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Lambda', 'Omega_log (K)', 'Tc (K)', 'Timestamp'])
        w.writeheader()
        w.writerows(rows)

def get_csv_full_info(name):
    if not os.path.exists(CSV_FILE):
        return {}
    for row in csv.DictReader(open(CSV_FILE, 'r')):
        if row['Name'] == name:
            return row
    return {}

# =============================================================================
# 3. PWSCF / PHONON ENGINE
# =============================================================================
def is_xml_valid(xml_path):
    if not os.path.exists(xml_path):
        return False
    try:
        with open(xml_path, 'rb') as f:
            f.seek(max(0, os.path.getsize(xml_path) - 1000))
            tail = f.read().decode('utf-8', errors='ignore')
        return "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail
    except:
        return False

def manage_rolling_checkpoints(work_dir):
    tmp_dir = os.path.join(work_dir, "tmp")
    bkp1 = os.path.join(work_dir, "tmp_SAFE_1")
    bkp2 = os.path.join(work_dir, "tmp_SAFE_2")
    if not os.path.exists(tmp_dir):
        return
    if os.path.exists(bkp1):
        if os.path.exists(bkp2):
            shutil.rmtree(bkp2, ignore_errors=True)
        try:
            shutil.move(bkp1, bkp2)
        except:
            pass
    try:
        shutil.copytree(tmp_dir, bkp1)
    except:
        pass

def restore_rolling_checkpoint(work_dir, prefix):
    tmp_dir = os.path.join(work_dir, "tmp")
    for bkp in [os.path.join(work_dir, "tmp_SAFE_1"), os.path.join(work_dir, "tmp_SAFE_2")]:
        if os.path.exists(bkp):
            print(f"      🔄 Versuche Wiederherstellung aus {os.path.basename(bkp)}...")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                shutil.copytree(bkp, tmp_dir)
                if is_xml_valid(os.path.join(tmp_dir, f"{prefix}.save", "data-file-schema.xml")):
                    return True
            except:
                pass
    return False

def make_kpoints_dense(filepath):
    if not os.path.exists(filepath):
        return False
    with open(filepath, 'r') as f:
        content = f.read()
    if "! KPOINTS_DENSIFIED" in content:
        return False 
    out_lines = []
    in_k = False
    for line in content.split('\n'):
        if "K_POINTS" in line.upper() and "automatic" in line.lower():
            in_k = True
            out_lines.append(line)
            continue
        if in_k and line.strip() and not line.strip().startswith("!"):
            p = line.split()
            if len(p) >= 3:
                out_lines.append(f" {max(6, int(p[0])*2)} {max(6, int(p[1])*2)} {max(6, int(p[2])*2)} {' '.join(p[3:]) if len(p)>3 else '0 0 0'} ! KPOINTS_DENSIFIED")
                in_k = False
                continue
        out_lines.append(line)
    with open(filepath, 'w') as f:
        f.write("\n".join(out_lines))
    return True

def analyze_crash_reason(output_file, start_size=0):
    if not os.path.exists(output_file):
        return "NONE"
    try:
        with open(output_file, 'rb') as f:
            f.seek(start_size)
            lines = f.read().decode('utf-8', errors='ignore').lower()
        if "job done" in lines:
            return "DONE"
        if "convergence not achieved" in lines:
            return "NON_CONVERGED"
        if "wrong trans" in lines or "wrong elph" in lines:
            return "WRONG_TRANS_ERROR"
        if "fatal error reading xml" in lines or "tag root not found" in lines or "xmltools.f90" in lines:
            return "XML_ERROR"
        if "not orthogonal" in lines and "d_s" in lines:
            return "SYMMETRY_ERROR"
        if "mx dimension too small" in lines:
            return "PSEUDO_ERROR"
        if "i/o past end of record" in lines or "end of file" in lines:
            return "ELPH_CORRUPT"
        
        error_keywords = ["error", "mpi_abort", "segmentation fault", "stopping", "fatal", "diagonalization failed"]
        has_error_msg = any(k in lines for k in error_keywords)

        if has_error_msg:
            return "HARD"
        
        ram_match = re.search(r"estimated total dynamical ram\s*>\s*([0-9\.]+)\s*(mb|gb)", lines)
        if ram_match:
            if "self-consistent calculation" not in lines and "iteration #" not in lines:
                return "LIKELY_OOM"
        if "iteration #" in lines or "diagonalization" in lines:
            if not has_error_msg:
                return "LIKELY_OOM"
        
        return "SOFT"
    except:
        return "HARD"
    
def run_monitored_pw(input_file, output_file, cwd, active_cores):
    with open(input_file, 'r') as f:
        content = f.read()
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{PSEUDO_DIR}/'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{PSEUDO_DIR}/',\n")
        
    if "electron_maxstep" in content:
        content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 150", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 150,\n")
    
    tmp_dir = os.path.join(cwd, "tmp") 
    prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
    prefix = prefix_match.group(1) if prefix_match else "calc"
    mode = 'restart' if os.path.exists(output_file) and is_xml_valid(os.path.join(tmp_dir, f"{prefix}.save", "data-file-schema.xml")) else 'from_scratch'
    
    if mode == 'from_scratch' and os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        
    if "restart_mode" in content:
        content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',\n")
    
    with open(input_file+".run", 'w') as f:
        f.write(content)
    
    start_size = os.path.getsize(output_file) if os.path.exists(output_file) else 0
    with open(input_file+".run", 'r') as f_in, open(output_file, 'a' if mode=='restart' else 'w') as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE, "-ndiag", "1"]
        print(f"      ⚙️ Starte PWSCF ({mode} {active_cores} Cores)...")
        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        try:
            while process.poll() is None:
                time.sleep(2)
        except:
            process.kill()
            return "CRASH"
        
        if process.returncode == -9:
            return "OOM"
        return analyze_crash_reason(output_file, start_size)

def execute_scf_block(name, scf_in, scf_out, work_dir, phase_label):
    if os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read():
        if not os.path.exists(os.path.join(work_dir, "tmp")):
            os.remove(scf_out)
        else:
            return "DONE", int(DEFAULT_CORES)
            
    update_csv(name, f"Rechnet SCF ({phase_label})...")
    oom_level = 0
    while True:
        cores = int(SAFE_CORES) if oom_level >= 4 else int(DEFAULT_CORES)
        print(f"   1️⃣  SCF {phase_label} ({cores} Cores, OOM-Lvl {oom_level})")
        res = run_monitored_pw(scf_in, scf_out, work_dir, cores)
        
        if res == "DONE":
            return "DONE", cores
        if res == "OOM" or res == "LIKELY_OOM":
            oom_level += 1
            if oom_level > 4: 
                send_notification(f"❌ SCF übersprungen bei {name} - Maximales OOM-Level erreicht.")
                return "SKIPPED", cores
            print(f"      ⚠️ OOM Fehler! Eskaliere auf Level {oom_level}")
            update_csv(name, f"Retrying (OOM Lvl {oom_level})")
            continue
        print("      ❌ SCF Crash.")
        return "SKIPPED", cores

def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_sync = time.time()
    last_cp = time.time()
    start_time = time.time()
    check_and_free_disk_space()

    with open(input_file, 'r') as f:
        content = f.read()
        
    ph0_dir = os.path.join(cwd, "tmp", "_ph0")
    if os.path.exists(ph0_dir):
        rec_mode = "recover=.true."
    else:
        rec_mode = "recover=.false."

    if "recover=" in content.lower():
        content = re.sub(r"recover\s*=\s*\.[a-zA-Z]+\.", rec_mode, content, flags=re.IGNORECASE)
    else:
        content = content.replace("&INPUTPH", f"&INPUTPH\n {rec_mode},\n")
        
    with open(input_file+".run", 'w') as f:
        f.write(content)
    
    file_mode = 'a' if ("recover=.true." in content.lower()) else 'w'
    start_size = os.path.getsize(output_file) if os.path.exists(output_file) and file_mode == 'a' else 0

    with open(input_file+".run", 'r') as f_in, open(output_file, file_mode) as f_out:
        process = subprocess.Popen(["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        print(f"      ⚙️ Starte PHONONEN ({active_cores} Cores, {rec_mode})...")
        try:
            while process.poll() is None:
                time.sleep(2)
                if time.time() - last_cp > 1800:
                    manage_rolling_checkpoints(cwd)
                    last_cp = time.time()
                if time.time() - last_sync > 1800:
                    git_sync("Heartbeat (Phonon Running)")
                    last_sync = time.time()

                try:
                    if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                        print("      ⚠️ RAM NOT-AUS!")
                        process.terminate()
                        return "OOM", time.time() - start_time
                except:
                    pass
        except: 
            process.kill()
            return "CRASH", time.time() - start_time
        
        run_duration = time.time() - start_time
        if process.returncode == -9:
            return "OOM", run_duration

        try:
            with open(output_file, 'rb') as f:
                f.seek(start_size)
                out_new = f.read().decode('utf-8', errors='ignore')
                if "JOB DONE" in out_new:
                    return "DONE", run_duration
        except:
            pass

        res = analyze_crash_reason(output_file, start_size)
        return res if res != "NONE" else "CRASH", run_duration

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    ensure_gitignore()
    print("☁️ Führe initialen Git Pull aus...")
    initial_git_pull()
    
    with open(TXT_LOG_FILE, "a") as f:
        f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE\n{'='*40}\n")
    print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE\n{'='*40}\n")

    send_notification("🚀 Smart-Pipeline wurde gestartet!")
    
    input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
    jobs_processed = 0
    
    for input_file in input_files:
        name = os.path.basename(input_file).replace(".in", "")
        work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
        row_data = get_csv_full_info(name)
        status_str = row_data.get('Status', '').upper()
        
        # Geänderte Logik: Nur bei bewussten Einträgen wie "SKIPPED" oder "Isolator" wird die Rechnung übersprungen.
        # Ein "FEHLER (...)" führt nun dazu, dass die Pipeline die Rechnung direkt fortsetzt.
        if status_str == "SKIPPED" or "ISOLATOR" in status_str:
            continue
        if row_data.get('Stabilität', '') == "INSTABIL":
            continue
        
        lam_val = row_data.get('Lambda', '').strip()
        if row_data.get('Stabilität', '') == "STABIL" and lam_val != "" and lam_val != "-":
            continue

        jobs_processed += 1

        # --- PHASE 2 (Präzisions-Modus) ---
        if not os.path.exists(work_dir):
            os.makedirs(work_dir)
        scf_in = os.path.join(work_dir, "scf.in")
        scf_out = os.path.join(work_dir, "scf.out")
        ph_in = os.path.join(work_dir, "ph.in")
        ph_out = os.path.join(work_dir, "ph.out")
        
        if not os.path.exists(scf_in):
            shutil.copy(input_file, scf_in)
        
        was_densified = make_kpoints_dense(scf_in)
        if was_densified:
            print("   🧹 Verdoppele K-Punkte für Präzision.")
            if os.path.exists(scf_out):
                os.remove(scf_out) 
            shutil.rmtree(os.path.join(work_dir, "tmp"), ignore_errors=True)
            shutil.rmtree(os.path.join(work_dir, "tmp_PRISTINE_PH"), ignore_errors=True)
            
        if os.path.exists(ph_in) and "electron_phonon" not in open(ph_in, errors='ignore').read():
            print("   ℹ️ Phase 2 erkannt - Warte auf manuellen Reset (Löschen der ph.in UND ph.out durch dich) falls nötig.")
        
        print(f"\n💎 Job, {name} (Phase 2)")
        scf_res, scf_cores = execute_scf_block(name, scf_in, scf_out, work_dir, "Präzision")
        if scf_res != "DONE":
            continue
        
        prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", open(scf_in).read())
        prefix = prefix_match.group(1) if prefix_match else "calc"
        update_csv(name, "Rechnet El-Ph...")

        ph_res = "DONE"
        run_duration = 0

        if not os.path.exists(ph_in) and not os.path.exists(ph_out):
            print("   🧹 Manueller Reset erkannt. Bereinige Phononen-Daten...")
            ph0_path = os.path.join(work_dir, "tmp", "_ph0")
            if os.path.exists(ph0_path):
                shutil.rmtree(ph0_path, ignore_errors=True)
            for ext in ["*.dvscf*", "*.a2Fsave*", "*.dyn*", "*.fc", "*.freq", "*.phdos"]:
                for f in glob.glob(os.path.join(work_dir, "tmp", ext)) + glob.glob(os.path.join(work_dir, ext)):
                    try:
                        os.remove(f)
                    except:
                        pass
            
            with open(ph_in, "w") as f:
                f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., fildvscf='dvscf', nq1=2, nq2=2, nq3=2, recover=.false., electron_phonon='interpolated' /\n")
            
            pristine = os.path.join(work_dir, "tmp_PRISTINE_PH")
            if not os.path.exists(pristine) and os.path.exists(os.path.join(work_dir, "tmp")):
                shutil.copytree(os.path.join(work_dir, "tmp"), pristine)

        if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
            ph_attempts = 0
            while ph_attempts < 5:
                ph_attempts += 1
                ph_res, run_duration = run_monitored_ph(ph_in, ph_out, work_dir, scf_cores)
                
                if ph_res == "DONE":
                    break
                
                error_msg = f"🧨 ERROR Phonon-Crash ({ph_res}) bei Job {name}. Stoppe Job für manuelle Analyse!"
                print(f"      {error_msg}")
                print_error_tail(ph_out, 50) 
                with open(TXT_LOG_FILE, "a") as f:
                    f.write(f"\n{error_msg}\n      => Die Dateien ph.in und ph.out wurden NICHT gelöscht.\n")
                break

        if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
             send_notification(f"❌ Job {name} pausiert wegen eines Phonon-Crashes ({ph_res}). Bitte manuell prüfen.")
             update_csv(name, f"FEHLER ({ph_res})")
             continue

        print("   ✅ El-Ph fertig. Starte Q2R und Matdyn...")
        # Hier folgt später die Logik für Q2R und Matdyn zur Tc-Berechnung.
        send_notification(f"✅ Job {name} (Phase 2) erfolgreich berechnet!")
        
    git_sync("🏁 Finaler Sync vor Shutdown (Erfolgreich)")
    
    with open(SIGNAL_FILE, "w") as f:
        f.write("Status, Fertig")

    if jobs_processed == 0:
        print("💡 Keine offene Berechnung gefunden. Alle Jobs sind bereits erledigt oder übersprungen.")
        send_notification("💡 Pipeline beendet - Keine offenen Berechnungen gefunden.")
    else:
        send_notification(f"🎉 Pipeline erfolgreich beendet! {jobs_processed} Job(s) verarbeitet.")

    if sys.stdout.isatty():
        print("🖥️ Interaktive Bash-Session erkannt. Automatischer Shutdown der VM wird übersprungen.")
    else:
        if os.name != 'nt': 
            print("🛑 Fordere sofortige Deallokierung bei Azure an...")
            try:
                token_url = "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F"
                headers = {"Metadata": "true"}
                token_resp = requests.get(token_url, headers=headers, timeout=10)
                
                if token_resp.status_code == 200:
                    access_token = token_resp.json()["access_token"]
                    
                    sub_id = "6cc5f986-8abd-4d35-84ac-63fdc737a1a5"
                    rg_name = "Supraleiter-HPC-Knoten_group"
                    vm_name = "Supraleiter-HPC-Knoten"
                    
                    dealloc_url = f"https://management.azure.com/subscriptions/{sub_id}/resourceGroups/{rg_name}/providers/Microsoft.Compute/virtualMachines/{vm_name}/deallocate?api-version=2022-03-01"
                    auth_headers = {"Authorization": f"Bearer {access_token}"}
                    
                    dealloc_resp = requests.post(dealloc_url, headers=auth_headers, timeout=10)
                    
                    if dealloc_resp.status_code in [200, 202]:
                        print("✅ VM wird nun von Azure komplett deallokiert. Tschüss!")
                        sys.exit(0)
                        
                print("⚠️ Managed Identity nicht konfiguriert. Greife auf normalen OS-Shutdown zurück.")
            except Exception as e:
                print(f"⚠️ Fehler bei der API-Deallokierung - {e}")
            
            os.system("sudo shutdown -h now")

if __name__ == "__main__":
    main()