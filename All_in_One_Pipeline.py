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
import signal
from datetime import datetime

# =============================================================================
# 0. LIVE-LOGGING
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def truncate_log(log_path, max_size_mb=1.0):
    if not os.path.exists(log_path): return
    max_bytes = int(max_size_mb * 1024 * 1024)
    if os.path.getsize(log_path) > max_bytes:
        try:
            with open(log_path, 'rb') as f:
                f.seek(-max_bytes, 2)
                content = f.read()
            with open(log_path, 'wb') as f:
                f.write(content)
            print(f"✂️ Logfile {os.path.basename(log_path)} auf {max_size_mb} MB gekürzt.")
        except Exception as e:
            print(f"⚠️ Konnte Logfile nicht kürzen, {e}")

# =============================================================================
# 1. KONFIGURATION
# =============================================================================
TELEGRAM_TOKEN = open("/home/marco/.telegram_token").read().strip()
TELEGRAM_CHAT_ID = "711461437"

LOGIC_APP_NAME = "AutoRestart-Supraleiter"
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"
DOS_THRESHOLD = 0.05

DEFAULT_CORES = "2"
SAFE_CORES = "1"
MEMORY_LIMIT_PERCENT = 88.0
MAX_BFGS_STEPS = 100 
MAX_RETRIES_LEVEL = 3
ERROR_LOG_LINES = 30

FORCE_RETRY_LIST = []

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")
BACKUP_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output_backup.txt")

PW_EXE = shutil.which("pw.x") or "/usr/bin/pw.x"
PH_EXE = shutil.which("ph.x") or "/usr/bin/ph.x"
DOS_EXE = shutil.which("dos.x") or "/usr/bin/dos.x"
Q2R_EXE = shutil.which("q2r.x") or "/usr/bin/q2r.x"
MATDYN_EXE = shutil.which("matdyn.x") or "/usr/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT
# =============================================================================
def backup_log_file():
    truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
    if os.path.exists(TXT_LOG_FILE):
        try: shutil.copy(TXT_LOG_FILE, BACKUP_LOG_FILE)
        except: pass

def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC, {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"): return
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except: pass

def initial_git_pull():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
    except Exception as e:
        print(f"⚠️ Initialer Git Pull Fehler, {e}")

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    lock_file = os.path.join(WORK_DIR, ".git", "index.lock")
    try:
        subprocess.run(["git", "config", "credential.helper", "store"], cwd=WORK_DIR, env=env, timeout=10)
        
        if os.path.exists(lock_file):
            lock_age = time.time() - os.path.getmtime(lock_file)
            if lock_age > 60:
                os.remove(lock_file)
                print("⚠️ Stale index.lock entfernt.")
                
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, env=env, timeout=30)
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
    except Exception as e:
        print(f"⚠️ Git Fehler, {e}")

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    try:
        lam = float(lambda_ep)
        wlog = float(omega_log_K)
        if lam <= 0:
            return 0.0
        vorfaktor = wlog / 1.20
        zaehler = -1.04 * (1.0 + lam)
        nenner = lam - mu_star * (1.0 + 0.62 * lam)
        if nenner <= 0:
            return 0.0
        return vorfaktor * math.exp(zaehler / nenner)
    except:
        return "-"

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-", lam="-", wlog="-", tc="-"):
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Lambda', 'Omega_log (K)', 'Tc (K)', 'Timestamp']
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f: 
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for ef in reader.fieldnames:
                    if ef not in fieldnames: fieldnames.append(ef)
            rows = list(reader)
            
    found = False
    for row in rows:
        if row['Name'] == name:
            row.update({'Status': status, 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")})
            if e_fermi != "-": row['Fermi Energie (eV)'] = str(e_fermi)
            if dos_val != "-": row['DOS @ Fermi'] = str(dos_val)
            if is_metal != "-": row['Metall?'] = str(is_metal)
            if min_f != "-": row['Min Freq (THz)'] = str(min_f)
            if stab != "-": row['Stabilität'] = str(stab)
            if lam != "-": row['Lambda'] = str(lam)
            if wlog != "-": row['Omega_log (K)'] = str(wlog)
            if tc != "-": row['Tc (K)'] = str(tc)
            found = True
            break
    if not found:
        new_row = {'Name': name, 'Status': status, 'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val), 'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f), 'Stabilität': str(stab), 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")}
        if lam != "-": new_row['Lambda'] = str(lam)
        if wlog != "-": new_row['Omega_log (K)'] = str(wlog)
        if tc != "-": new_row['Tc (K)'] = str(tc)
        rows.append(new_row)
        
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def get_csv_full_info(name):
    if not os.path.exists(CSV_FILE): return {}
    with open(CSV_FILE, 'r') as f:
        for row in csv.DictReader(f):
            if row['Name'] == name: return row
    return {}

def count_job_attempts(log_file, job_name):
    if not os.path.exists(log_file): return 1
    count = 0
    try:
        with open(log_file, 'rb') as f:
            f.seek(0, 2) 
            size = f.tell()
            f.seek(max(0, size - 50000), 0)
            lines = f.read().decode('utf-8', errors='ignore').splitlines()
        job_marker = f"💎 Job, {job_name}"
        for line in reversed(lines):
            if job_marker in line:
                count += 1
            elif "💎 Job," in line and job_name not in line:
                break
    except: return 1
    return max(1, count)

def cleanup_heavy_files(work_dir, name, force=False):
    if not force:
        row_data = get_csv_full_info(name)
        if not row_data:
            print(f"      ⚠️ Cleanup abgebrochen, {name} nicht in CSV gefunden.")
            return
        
        status = row_data.get('Status', '')
        tc_val = row_data.get('Tc (K)', '-')
        stab = row_data.get('Stabilität', '-')
        
        is_finished = ("Isolator" in status) or (stab == "INSTABIL") or (tc_val != "-")
        
        if not is_finished:
            return

    deleted_something = False
    
    for dvscf_file in glob.glob(os.path.join(work_dir, "*dvscf*")):
        try:
            os.remove(dvscf_file)
            deleted_something = True
        except: pass

    for path in [os.path.join(work_dir, p) for p in ["tmp", "tmp_SAFE_CHECKPOINT", "tmp_SAFE_PHONON_CHECKPOINT"]]:
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
                deleted_something = True
            except Exception as e:
                print(f"      ⚠️ Konnte {path} nicht löschen, {e}")
                
    if deleted_something:
        print(f"      🧹 Heavy Files & dvscf für {name} sicher bereinigt.")
        git_sync(f"Cleanup Heavy Files, {name}")

def print_error_log(output_file, label="QE ERROR LOG"):
    if not os.path.exists(output_file): return
    try:
        err_lines = open(output_file, errors='ignore').read().strip().split('\n')
        snippet   = err_lines[-ERROR_LOG_LINES:]
        print(f"      --- {label} ---")
        print("      " + "\n      ".join(snippet))
        print("      " + "-" * 20)
    except: pass

# =============================================================================
# 3. SMART LOGIC & VALIDATION & CRASH ANALYSE
# =============================================================================

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try: f.seek(-20000, 2)
            except OSError: f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore')
        
        if "JOB DONE" in lines: return "DONE"
        if "convergence NOT achieved" in lines: return "NON_CONVERGED"
        
        lines_lower = lines.lower()
        
        if "fatal error reading xml" in lines_lower or "reading output_obj of xsd" in lines_lower or "wrong number of occurrences" in lines_lower:
            print("      🧨 XML-Struktur zerstört (Corruption).")
            return "XML_ERROR"
            
        if "wavefunctions in collected format not available" in lines_lower:
            print("      ⚠️ Wellenfunktionen fehlen (wf_collect Error).")
            return "WF_COLLECT_ERROR"

        if "i/o past end of record" in lines_lower or ("end of file" in lines_lower and ("elphon.f90" in lines_lower or "write_rec.f90" in lines_lower)):
            print("      ⚠️ Korrupte Lese-/Schreibdatei (I/O Error).")
            return "CORRUPT_FILE_ERROR"

        if "not orthogonal" in lines_lower and "d_s" in lines_lower:
            print("      🧩 Symmetrie-Fehler erkannt (D_S not orthogonal).")
            return "SYMMETRY_ERROR"
            
        if "fft grid incompatible with symmetry" in lines_lower:
            print("      🧩 FFT-Gitter Inkompatibilität (Symmetrie-Konflikt).")
            return "FFT_SYMMETRY_ERROR"

        if "error reading file" in lines_lower and "xml" not in lines_lower:
            print("      🤕 Fragmentierungsfehler (davcio).")
            return "DAVCIO_ERROR"

        if "aainit" in lines_lower and "mx dimension too small" in lines_lower:
            print("      🔩 aainit-Fehler erkannt (MPI-Bug oder RAM).")
            return "AAINIT_ERROR"

        if "mx dimension too small" in lines_lower:
            print("      🧨 FATAL, Pseudopotential übersteigt QE-Limit. Neues Pseudo (PAW) benötigt!")
            return "PSEUDO_ERROR"

        ram_match = re.search(r"estimated total dynamical ram\s*>\s*([0-9\.]+)\s*(mb|gb)", lines_lower)

        error_keywords = ["error", "mpi_abort", "segmentation fault", "stopping", "fatal", "diagonalization failed"]
        has_error_msg = any(key in lines_lower for key in error_keywords)

        if has_error_msg: 
            return "HARD"

        if ram_match:
            if "self-consistent calculation" not in lines_lower and "iteration #" not in lines_lower:
                return "LIKELY_OOM"

        if "iteration #" in lines_lower or "diagonalization" in lines_lower:
            if not has_error_msg:
                return "LIKELY_OOM"
        
        return "SOFT"
    except: return "HARD"
    
def cleanup_system_memory():
    print("      🧹 Bereinige Zombie-Prozesse und Shared Memory (/dev/shm)...")
    target_procs = ['pw.x', 'ph.x', 'dos.x', 'q2r.x', 'matdyn.x']
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] in target_procs:
                proc.kill()
        except:
            pass

    shm_dir = "/dev/shm"
    if os.path.exists(shm_dir):
        for item in os.listdir(shm_dir):
            item_path = os.path.join(shm_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except:
                pass
    print("      ✅ System-RAM und Prozesse sind sauber.")

def run_monitored_pw(input_file, output_file, cwd, active_cores):
    fix_input_file(input_file, 0)
    last_git_sync = time.time()
    last_checkpoint_time = 0 

    while True:
        with open(input_file, 'r') as f: content = f.read()
        tmp_dir = os.path.join(cwd, "tmp") 
        checkpoint_dir = os.path.join(cwd, "tmp_SAFE_CHECKPOINT") 

        if "wf_collect" in content:
            content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?", "wf_collect=.true.", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

        prefix_match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
        current_prefix = prefix_match.group(1) if prefix_match else "calc"
        xml_path = os.path.join(tmp_dir, f"{current_prefix}.save", "data-file-schema.xml")
        
        mode = 'from_scratch'
        if os.path.exists(output_file) and is_xml_valid(xml_path):
            mode = 'restart'
            print("      ✅ Gültige XML im tmp-Ordner gefunden -> Normaler Restart.")
        elif os.path.exists(output_file) and os.path.exists(checkpoint_dir):
            print("      🛡️ tmp-Ordner defekt/unvollständig! Hole Safe-Checkpoint...")
            try:
                if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
                shutil.copytree(checkpoint_dir, tmp_dir)
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint erfolgreich geladen!")
                else:
                    print("      ❌ Checkpoint war auch defekt. Starte von vorne.")
            except Exception as e: print(f"      ❌ Fehler beim Laden des Checkpoints, {e}")
        else:
            print("      🆕 Kein gültiger Speicherstand gefunden -> Starte von vorne (From Scratch).")

        if mode == 'from_scratch':
            if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir, ignore_errors=True)
            if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir, ignore_errors=True)

        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        backup_log_file()
        cleanup_system_memory()
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE, "-ndiag", "1"]
            print(f"      ⚙️ Starte PWSCF ({mode}, {active_cores} Cores, -ndiag 1)...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd, start_new_session=True)
            
            try:
                while process.poll() is None:
                    time.sleep(5)
                    
                    if time.time() - last_checkpoint_time > 900: 
                        if is_xml_valid(xml_path):
                            print("      💾 XML valide -> Erstelle Checkpoint...")
                            try:
                                if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir)
                                shutil.copytree(tmp_dir, checkpoint_dir)
                                last_checkpoint_time = time.time()
                                print("      ✅ Checkpoint erstellt.")
                                git_sync("Checkpoint & Log Update")
                                backup_log_file()
                                last_git_sync = time.time() 
                            except Exception as e: print(f"      ⚠️ Checkpoint fail, {e}")

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        backup_log_file()
                        last_git_sync = time.time()

                    try:
                        mem_usage = psutil.virtual_memory().percent
                        if mem_usage > MEMORY_LIMIT_PERCENT:
                            print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                            return "OOM" 
                    except: pass

                    cur_iter = get_last_iteration(output_file)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 Limit erreicht ({cur_iter}/{MAX_BFGS_STEPS} BFGS Schritte). Breche ab.")
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        return "MAX_STEPS"
                    
                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except: 
                try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except: pass
                return "CRASH"
            
            time.sleep(1.5)
            
            if process.returncode == -9:
                print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
                return "OOM"

            if process.returncode != 0:
                reason = analyze_crash_reason(output_file)
                if reason == "LIKELY_OOM":
                    print("      💀 Logfile endet abrupt (Silent Death) -> OOM.")
                    return "OOM"
                return "CRASH"

            final_reason = analyze_crash_reason(output_file)
            if final_reason == "DONE": return "DONE"
            elif final_reason == "LIKELY_OOM": return "OOM"
            
            return "CRASH"
        
def is_xml_valid(xml_path):
    if not os.path.exists(xml_path): return False
    try:
        with open(xml_path, 'rb') as f:
            try: f.seek(-1000, 2) 
            except: f.seek(0)
            tail = f.read().decode('utf-8', errors='ignore')
        if "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail:
            return True
        return False
    except:
        return False

def is_recoverable_fragmentation_error(ph_output_file):
    if not os.path.exists(ph_output_file): return False
    try:
        with open(ph_output_file, 'r', errors='ignore') as f:
            content = f.read()
        if "mismatch in number of G-vectors" in content or ("error reading file" in content and "xml" not in content):
            return True
        return False
    except: return False

def run_cleanup_scf(scf_input_file, cwd, cores_to_use=2):
    print(f"      🚑 Starte RECOVERY-Modus (Collect Waves), Cores={cores_to_use}")
    
    with open(scf_input_file, 'r') as f: content = f.read()
    
    if "restart_mode" in content:
        content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", "restart_mode='restart'", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n restart_mode='restart',")
        
    if "wf_collect" in content:
        content = re.sub(r"wf_collect\s*=\s*\.?[a-zA-Z]+\.?", "wf_collect=.true.", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n wf_collect=.true.,")

    if "nstep" in content:
        content = re.sub(r"nstep\s*=\s*\d+", "nstep=0", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n nstep=0,")

    recover_in = scf_input_file + ".recover"
    recover_out = os.path.join(cwd, "scf_recover.out")
    with open(recover_in, 'w') as f: f.write(content)
    
    cleanup_system_memory()
    with open(recover_in, 'r') as f_in, open(recover_out, 'w') as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(cores_to_use), PW_EXE]
        try:
            subprocess.run(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd, timeout=300)
            print("      ✅ Recovery-Lauf beendet. Daten sollten jetzt consolidated sein.")
            return True
        except Exception as e:
            print(f"      ❌ Recovery fehlgeschlagen, {e}")
            return False

def disable_symmetries_and_reduce_grid(input_file):
    if not os.path.exists(input_file): return
    with open(input_file, 'r') as f: content = f.read()
    
    content = content.replace("noinv=.true.,", "")
    content = content.replace("noinv=.true.", "")

    if "&INPUTPH" in content:
        if "search_sym" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n search_sym=.false.,")
    
    with open(input_file, 'w') as f: f.write(content)
    print("      🛡️ Symmetrien deaktiviert.")

def detect_oom_level(input_file):
    if not os.path.exists(input_file): return 0
    with open(input_file, 'r', errors='ignore') as f: content = f.read()
    if "mixing_ndim = 2" in content or "mixing_ndim=2" in content: return 4
    if "mixing_ndim = 3" in content or "mixing_ndim=3" in content: return 3
    if "disk_io='low'" in content or 'disk_io="low"' in content: return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content: return 1
    return 0

def apply_oom_settings(input_file, level):
    with open(input_file, 'r') as f: content = f.read()
    diag = 'david'; mix = 6; disk = None 
    msg = "Standard (david, mix=6, diago_david_ndim=2)"

    if level >= 1: diag = 'cg'; mix = 4; disk = 'low'; msg = "Stufe 1 (cg, mix=4, disk_io='low')"
    if level >= 2: diag = 'cg'; mix = 3; disk = 'low'; msg = "Stufe 2 (cg, mix=3, disk_io='low')"
    if level >= 3: diag = 'cg'; mix = 2; disk = 'low'; msg = "Stufe 3 (cg, mix=2, disk_io='low')"
    if level >= 4: diag = 'cg'; mix = 2; disk = 'low'; msg = "Stufe 4 (cg, mix=2, disk_io='low', 1 Core)"

    print(f"      📉 Setze RAM-Strategie, {msg}")

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")

    ndim = 4 if level == 0 else 2
    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+", f"diago_david_ndim = {ndim}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diago_david_ndim = {ndim},")

    if disk == 'low':
        if "disk_io" in content:
            content = re.sub(r"disk_io\s*=\s*['\"][a-zA-Z]+['\"]", "disk_io='low'", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n disk_io='low',")
    else:
        if "disk_io='low'" in content or 'disk_io="low"' in content:
             content = re.sub(r"disk_io\s*=\s*['\"]low['\"],?", "", content)

    with open(input_file, 'w') as f: f.write(content)
    return True

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

    if "mixing_mode" in content:
        content = re.sub(r"mixing_mode\s*=\s*['\"][a-zA-Z\-]+['\"]", "mixing_mode='local-TF'", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_mode='local-TF',")

    if "ecutwfc" in content:
        content = re.sub(r"ecutwfc\s*=\s*[0-9\.]+", "ecutwfc = 80.0", content)
    if "ecutrho" in content:
        content = re.sub(r"ecutrho\s*=\s*[0-9\.]+", "ecutrho = 800.0", content)

    target_beta = 0.7
    if iteration_count >= 30: target_beta = 0.4
    if iteration_count >= 60: target_beta = 0.25
    if iteration_count >= 90: target_beta = 0.15

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_beta = {target_beta},")
    
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


# --- ROBUSTE PHONON WRAPPER ---
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync = time.time()

    with open(input_file, 'r') as f: content = f.read()
    if os.path.exists(output_file):
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")
    
    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)
    
    file_mode = 'a' if "recover=.true." in content else 'w'
    backup_log_file()
    cleanup_system_memory()

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN (Cores, {active_cores})...")
        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd, start_new_session=True)
        
        try:
            while process.poll() is None:
                time.sleep(5)
                
                if time.time() - last_git_sync > 1800:
                    print("      ❤️ Git Heartbeat (Phonon)...")
                    git_sync("Log Update (Phonon Running)")
                    backup_log_file()
                    last_git_sync = time.time()

                try:
                    mem_usage = psutil.virtual_memory().percent
                    if mem_usage > MEMORY_LIMIT_PERCENT:
                        print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        return "OOM"
                except: pass

        except: 
            try: os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except: pass
            return "CRASH"
        
        time.sleep(1.5)
        
        if process.returncode == -9:
            print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            return "OOM"

        if process.returncode != 0:
            return "CRASH"

        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read(): return "DONE"
        except: pass
        
        return "CRASH"
    
def deallocate_vm():
    if not shutil.which("az"): 
        print("🛑 Azure CLI nicht gefunden. Verlasse mich auf lokalen Shutdown.")
        return
    try:
        result = subprocess.run(["az", "vm", "deallocate", "--resource-group", RESOURCE_GROUP, "--name", "Supraleiter-HPC-Knoten"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"⚠️ Azure CLI Deallocate Fehler, {result.stderr}")
        else:
            print("✅ VM erfolgreich deallokiert.")
    except Exception as e: 
        print(f"⚠️ Fehler beim Aufruf der Azure CLI, {e}")

def is_ssh_session_active():
    try:
        output = subprocess.check_output(["who"]).decode("utf-8")
        return "pts/" in output or "tty" in output
    except Exception:
        return False

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        print("☁️ Führe initialen Git Pull aus...")
        initial_git_pull()
        
        set_logic_app_state("Enabled")
        
        truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        cleanup_system_memory()

        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start, {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out = os.path.join(work_dir, "scf.out")
            
            if name in FORCE_RETRY_LIST:
                print(f"🔄 ERZWUNGENER NEUSTART für {name} (Lösche korrupten RUN-Ordner)...")
                if os.path.exists(work_dir):
                    shutil.rmtree(work_dir, ignore_errors=True)
                update_csv(name, "NEW", "-", "-", "-", "-", "-")
            
            row_data = get_csv_full_info(name)
            last_status = row_data.get('Status', 'NEW')
            stability = row_data.get('Stabilität', '-')

            if "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status, {last_status})")
                continue
            
            if "Isolator" in last_status:
                cleanup_heavy_files(work_dir, name)
                print(f"⏩ Überspringe {name} (Ist ein Isolator)")
                continue

            if "Metall" in last_status and stability in ["STABIL", "INSTABIL"]:
                cleanup_heavy_files(work_dir, name)
                print(f"⏩ Überspringe {name} (Bereits vollständig analysiert, {stability})")
                continue

            if "Metall" in last_status and (stability == "-" or stability == "Unbekannt"):
                print(f"🔄 Retry Phonon für {name} (Metall, aber Stabilität unbekannt)...")

            crash_type = analyze_crash_reason(scf_out)
            if crash_type == "NON_CONVERGED":
                update_csv(name, "SKIPPED (Non-Conv)")
                continue
            elif crash_type == "DONE":
                print(f"✅ {name} SCF ist fertig.")
            
            try:
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                print(f"\n💎 Job, {name}")
                scf_in = os.path.join(work_dir, "scf.in")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                result = "DONE"
                crash_counter = 0

                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read()):
                    update_csv(name, "Rechnet SCF...")
                    
                    file_level = detect_oom_level(scf_in)
                    start_crash_reason = analyze_crash_reason(scf_out)
                    
                    if start_crash_reason == "LIKELY_OOM":
                        attempts = count_job_attempts(TXT_LOG_FILE, name)
                        print(f"      🕵️ OOM-Signatur vom letzten Lauf erkannt. Versuch Nr. {attempts} auf diesem Level.")
                        
                        if os.path.exists(scf_out):
                            timestamp = datetime.now().strftime("%H%M%S")
                            new_name = f"{scf_out}.crash_{timestamp}"
                            try:
                                os.rename(scf_out, new_name)
                                print(f"      👻 Ghost-Protection, Alte scf.out zu {os.path.basename(new_name)} verschoben.")
                            except: pass

                        if attempts >= MAX_RETRIES_LEVEL:
                            oom_level = file_level + 1
                            print(f"      ❗ Threshold ({MAX_RETRIES_LEVEL}) erreicht! Eskaliere HÄRTER (Level {file_level} -> {oom_level}).")
                            update_csv(name, f"Recovering (Escalating to Lvl {oom_level})")
                        else:
                            oom_level = file_level
                            print(f"      🔄 Threshold noch nicht erreicht. Gebe Level {file_level} noch eine Chance.")
                            update_csv(name, f"Retrying (Attempt {attempts}/{MAX_RETRIES_LEVEL})")
                    else:
                        oom_level = file_level

                    current_cores = int(DEFAULT_CORES)
                    if oom_level >= 4: current_cores = int(SAFE_CORES)
                    
                    crash_counter = 0  
                    
                    while True:
                        apply_oom_settings(scf_in, oom_level)
                        
                        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
                        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)
                        
                        if result == "DONE": break 
                        
                        elif result == "MAX_STEPS":
                            update_csv(name, "SKIPPED (Max BFGS Steps)")
                            git_sync(f"Skipped {name}, >{MAX_BFGS_STEPS} BFGS Steps")
                            break

                        elif result == "OOM":
                            oom_level += 1
                            crash_counter = 0 
                            print(f"      ⚠️ OOM Fehler erkannt. Eskaliere zu Level {oom_level}...")
                            
                            if oom_level == 1: update_csv(name, "Retrying (OOM Lvl 1, CG)")
                            elif oom_level == 2: update_csv(name, "Retrying (OOM Lvl 2, DiskIO)")
                            elif oom_level == 3: update_csv(name, "Retrying (OOM Lvl 3, Mix3)")
                            elif oom_level == 4:
                                update_csv(name, "Retrying (OOM Lvl 4, 1Core)")
                                current_cores = int(SAFE_CORES)
                            else:
                                update_csv(name, "SKIPPED (OOM Limit)")
                                print("      ❌ System zu komplex für verfügbaren RAM. Skippe.")
                                break
                            continue

                        elif result == "CRASH":
                            reason = analyze_crash_reason(scf_out)
                            print_error_log(scf_out)
                            
                            if reason == "NON_CONVERGED":
                                update_csv(name, "SKIPPED (Non-Conv)")
                                break
                            elif reason == "PSEUDO_ERROR":
                                update_csv(name, "SKIPPED (Pseudo Limit)")
                                print(f"      ❌ Skippe Job wegen inkompatiblem Pseudopotential.")
                                git_sync(f"Skipped {name}, Pseudo Error")
                                break
                            elif reason == "AAINIT_ERROR":
                                if current_cores > 1:
                                    print("      🔩 aainit-Fehler -> LÖSCHE tmp und Checkpoints, wechsle auf 1 Core.")
                                    current_cores = 1
                                    crash_counter = 0
                                    tmp_path = os.path.join(work_dir, "tmp")
                                    chkpt_path = os.path.join(work_dir, "tmp_SAFE_CHECKPOINT")
                                    if os.path.exists(tmp_path): shutil.rmtree(tmp_path, ignore_errors=True)
                                    if os.path.exists(chkpt_path): shutil.rmtree(chkpt_path, ignore_errors=True)
                                    update_csv(name, "Retrying (aainit -> 1 Core)")
                                    continue
                                else:
                                    print("      ❌ aainit-Fehler unlösbar. System zu komplex. Skippe.")
                                    update_csv(name, "SKIPPED (OOM Limit)")
                                    break
                            else:
                                crash_counter += 1
                                if crash_counter >= 3:
                                    print(f"      ❌ Zu viele unlösbare Abstürze ({crash_counter}). Skippe Job.")
                                    update_csv(name, "SKIPPED (Permanent Crash)")
                                    git_sync(f"Skipped {name}, Permanent Crash")
                                    break
                                
                                update_csv(name, f"Retrying (Crash {crash_counter}/3)")
                                time.sleep(2)
                                continue 

                if result == "MAX_STEPS" or result == "OOM" or crash_counter >= 3 or analyze_crash_reason(scf_out) in ["PSEUDO_ERROR", "AAINIT_ERROR"]: continue 
                if analyze_crash_reason(scf_out) != "DONE":
                    git_sync(f"Failed, {name}")
                    continue 

                with open(scf_in, 'r') as f: 
                    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                    prefix = match.group(1) if match else "calc"
                
                e_fermi = "-"
                if os.path.exists(scf_out):
                    with open(scf_out, 'r', errors='ignore') as f:
                        match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", f.read())
                        if match: e_fermi = float(match.group(1))

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
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")
                
                if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                    if not os.path.exists(ph_in):
                        with open(ph_in, "w") as f: 
                            f.write(f"Phons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., nq1=2, nq2=2, nq3=2 /\n")