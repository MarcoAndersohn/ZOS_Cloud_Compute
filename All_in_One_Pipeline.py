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
# 0. LIVE-LOGGING & TRUNCATE
# =============================================================================
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def truncate_log(log_path, max_size_mb=1.0):
    if not os.path.exists(log_path):
        return
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

def backup_log_file():
    truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
    if os.path.exists(TXT_LOG_FILE):
        try:
            shutil.copy(TXT_LOG_FILE, BACKUP_LOG_FILE)
        except:
            pass

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

PW_EXE = shutil.which("pw.x") or "/home/marco/qe-source/bin/pw.x"
PH_EXE = shutil.which("ph.x") or "/home/marco/qe-source/bin/ph.x"
DOS_EXE = shutil.which("dos.x") or "/usr/bin/dos.x"
Q2R_EXE = shutil.which("q2r.x") or "/usr/bin/q2r.x"
MATDYN_EXE = shutil.which("matdyn.x") or "/usr/bin/matdyn.x"

# =============================================================================
# 2. HELFER & GIT & SYSTEM CLEANUP
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC, {message}"}
        requests.post(url, data=payload, timeout=10)
    except:
        pass

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"):
        return
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except:
        pass

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    lock_file = os.path.join(WORK_DIR, ".git", "index.lock")
    try:
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

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-", lam="-", wlog="-", tc="-"):
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Lambda', 'Omega_log (K)', 'Tc (K)', 'Timestamp']
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'r') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for ef in reader.fieldnames:
                    if ef not in fieldnames:
                        fieldnames.append(ef)
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
        new_row = {
            'Name': name, 'Status': status,
            'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val),
            'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f),
            'Stabilität': str(stab),
            'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        if lam != "-": new_row['Lambda'] = str(lam)
        if wlog != "-": new_row['Omega_log (K)'] = str(wlog)
        if tc != "-": new_row['Tc (K)'] = str(tc)
        rows.append(new_row)

    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def get_csv_full_info(name):
    if not os.path.exists(CSV_FILE):
        return {}
    with open(CSV_FILE, 'r') as f:
        for row in csv.DictReader(f):
            if row['Name'] == name:
                return row
    return {}

def count_job_attempts(log_file, job_name):
    if not os.path.exists(log_file):
        return 1
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
    except:
        return 1
    return max(1, count)

def get_scf_cores(scf_out_path, default_cores=2):
    if not os.path.exists(scf_out_path): 
        return int(default_cores)
    try:
        with open(scf_out_path, 'r', errors='ignore') as f:
            matches = re.findall(r"running on\s+(\d+)\s+processors", f.read())
            if matches:
                return int(matches[-1])
    except: pass
    return int(default_cores)

def print_error_log(output_file, label="QE ERROR LOG"):
    if not os.path.exists(output_file): return
    try:
        err_lines = open(output_file, errors='ignore').read().strip().split('\n')
        snippet   = err_lines[-ERROR_LOG_LINES:]
        print(f"      --- {label} ---")
        print("      " + "\n      ".join(snippet))
        print("      " + "-" * 20)
    except: pass

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

def cleanup_heavy_files(work_dir, name):
    deleted_something = False
    for dvscf_file in glob.glob(os.path.join(work_dir, "*dvscf*")):
        try:
            os.remove(dvscf_file)
            deleted_something = True
        except:
            pass

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

def is_ssh_session_active():
    try:
        output = subprocess.check_output(["who"]).decode("utf-8")
        return "pts/" in output or "tty" in output
    except Exception:
        return False

# =============================================================================
# 3. SMART LOGIC & VALIDATION & CRASH ANALYSE
# =============================================================================

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file):
        return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try:
                f.seek(-20000, 2)
            except OSError:
                f.seek(0)
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

        if "i/o past end of record" in lines_lower or ("end of file" in lines_lower and ("elphon" in lines_lower or "write_rec" in lines_lower)):
            print("      ⚠️ Korrupte Lese-/Schreibdatei (I/O Error).")
            return "CORRUPT_FILE_ERROR"

        if "not orthogonal" in lines_lower and "d_s" in lines_lower:
            print("      🧩 Symmetrie-Fehler erkannt (D_S not orthogonal).")
            return "SYMMETRY_ERROR"

        if "error reading file" in lines_lower and "xml" not in lines_lower:
            print("      🤕 Fragmentierungsfehler (davcio).")
            return "DAVCIO_ERROR"

        if "mx dimension too small" in lines_lower:
            if "aainit" in lines_lower:
                print("      🔩 aainit-Fehler erkannt.")
                return "AAINIT_ERROR"
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
    except:
        return "HARD"

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
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir)
                shutil.copytree(checkpoint_dir, tmp_dir)
                if is_xml_valid(xml_path):
                    mode = 'restart'
                    print("      ✅ Checkpoint erfolgreich geladen!")
                else:
                    print("      ❌ Checkpoint war auch defekt. Starte von vorne.")
            except Exception as e:
                print(f"      ❌ Fehler beim Laden des Checkpoints, {e}")
        else:
            print("      🆕 Kein gültiger Speicherstand gefunden -> Starte von vorne (From Scratch).")

        if mode == 'from_scratch':
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if os.path.exists(checkpoint_dir):
                shutil.rmtree(checkpoint_dir, ignore_errors=True)

        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")

        run_input = input_file + ".run"
        with open(run_input, 'w') as f:
            f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        backup_log_file()
        cleanup_system_memory()

        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE, "-ndiag", "1"]
            print(f"      ⚙️ Starte PWSCF ({mode}, {active_cores} Cores, -ndiag 1)...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)

            try:
                while process.poll() is None:
                    time.sleep(5)

                    if time.time() - last_checkpoint_time > 900:
                        if is_xml_valid(xml_path):
                            print("      💾 XML valide -> Erstelle Checkpoint...")
                            try:
                                if os.path.exists(checkpoint_dir):
                                    shutil.rmtree(checkpoint_dir)
                                shutil.copytree(tmp_dir, checkpoint_dir)
                                last_checkpoint_time = time.time()
                                print("      ✅ Checkpoint erstellt.")
                                git_sync("Checkpoint & Log Update")
                                backup_log_file()
                                last_git_sync = time.time()
                            except Exception as e:
                                print(f"      ⚠️ Checkpoint fail, {e}")

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        backup_log_file()
                        last_git_sync = time.time()

                    try:
                        mem_usage = psutil.virtual_memory().percent
                        if mem_usage > MEMORY_LIMIT_PERCENT:
                            print(f"      ⚠️ RAM NOT-AUS (Python Monitor)!")
                            process.kill()
                            return "OOM"
                    except:
                        pass

                    cur_iter = get_last_iteration(output_file)
                    if cur_iter >= MAX_BFGS_STEPS:
                        print(f"      🛑 Limit erreicht ({cur_iter}/{MAX_BFGS_STEPS} BFGS Schritte). Breche ab.")
                        process.kill()
                        return "MAX_STEPS"

                    if cur_iter > 30:
                        fix_input_file(input_file, cur_iter)

            except:
                process.kill()
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
    if not os.path.exists(xml_path):
        return False
    try:
        with open(xml_path, 'rb') as f:
            try:
                f.seek(-1000, 2)
            except:
                f.seek(0)
            tail = f.read().decode('utf-8', errors='ignore')
        if "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail:
            return True
        return False
    except:
        return False

def is_recoverable_fragmentation_error(ph_output_file):
    if not os.path.exists(ph_output_file):
        return False
    try:
        with open(ph_output_file, 'r', errors='ignore') as f:
            content = f.read()
        if "mismatch in number of G-vectors" in content or ("error reading file" in content and "xml" not in content):
            return True
        return False
    except:
        return False

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
    with open(recover_in, 'w') as f:
        f.write(content)

    with open(recover_in, 'r') as f_in, open(recover_out, 'w') as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(cores_to_use), PW_EXE]
        try:
            subprocess.run(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd, timeout=300)
            print("      ✅ Recovery-Lauf beendet. Daten sollten jetzt consolidated sein.")
            return True
        except Exception as e:
            print(f"      ❌ Recovery fehlgeschlagen, {e}")
            return False

def detect_oom_level(input_file):
    if not os.path.exists(input_file):
        return 0
    with open(input_file, 'r', errors='ignore') as f:
        content = f.read()
    if "disk_io='low'" in content or 'disk_io="low"' in content:
        return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content:
        return 1
    return 0

def apply_oom_settings(input_file, level):
    with open(input_file, 'r') as f: content = f.read()
    diag = 'david'
    mix = 6
    disk = None
    msg = "Standard (david, mix=6, ndim=2)"

    if level >= 1:
        diag = 'cg'
        mix = 4
        msg = "Stufe 1 (cg, mix=4)"
    if level >= 2:
        mix = 3
        disk = 'low'
        msg = "Stufe 2 (cg, mix=3, disk_io='low')"
    if level >= 3:
        mix = 2
        disk = 'low'
        msg = "Stufe 3 (cg, mix=2, disk_io='low')"
    if level >= 4:
        mix = 2
        disk = 'low'
        msg = "Stufe 4 (cg, mix=2, disk_io='low', 1 Core)"

    print(f"      📉 Setze RAM-Strategie, {msg}")

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")

    if "diago_david_ndim" in content:
        content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")

    if disk == 'low':
        if "disk_io" in content:
            content = re.sub(r"disk_io\s*=\s*['\"][a-zA-Z]+['\"]", "disk_io='low'", content)
        else:
            content = content.replace("&CONTROL", "&CONTROL\n disk_io='low',")
    else:
        if "disk_io='low'" in content or 'disk_io="low"' in content:
             content = re.sub(r"disk_io\s*=\s*['\"]low['\"],?", "", content)

    with open(input_file, 'w') as f:
        f.write(content)
    return True

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{corr_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")

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

    if "electron_maxstep" in content:
        content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 150", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 150,")

    with open(input_file, 'w') as f:
        f.write(content)
    return True

def get_last_iteration(output_file):
    if not os.path.exists(output_file):
        return 0
    try:
        file_size = os.path.getsize(output_file)
        with open(output_file, 'rb') as f:
            f.seek(max(0, file_size - 10000), 0)
            chunk = f.read().decode('utf-8', errors='ignore')
        bfgs_matches = re.findall(r"number of bfgs steps\s*=\s*(\d+)", chunk)
        scf_matches = re.findall(r"iteration #\s*(\d+)", chunk)
        val = 0
        if bfgs_matches:
            val = int(bfgs_matches[-1])
        elif scf_matches:
            val = int(scf_matches[-1])
        return val
    except:
        return 0

# --- ROBUSTE PHONON WRAPPER ---
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync = time.time()

    with open(input_file, 'r') as f: content = f.read()
    if os.path.exists(output_file):
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")

    run_input = input_file + ".run"
    with open(run_input, 'w') as f:
        f.write(content)

    file_mode = 'a' if "recover=.true." in content else 'w'
    backup_log_file()
    cleanup_system_memory()

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN (Cores, {active_cores})...")
        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)

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
                        process.kill()
                        return "OOM"
                except:
                    pass
        except:
            process.kill()
            return "CRASH"

        time.sleep(1.5)

        if process.returncode == -9:
            print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            return "OOM"

        if process.returncode != 0:
            return "CRASH"

        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read():
                    return "DONE"
        except:
            pass

        return "CRASH"

# =============================================================================
# 3.5 PHONON BLOCK (2-PHASEN LOGIK)
# =============================================================================
def run_phonon_block(name, work_dir, scf_in, scf_out, ph_in, ph_out, e_fermi, dos_val):
    with open(scf_in, 'r') as f:
        match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
        prefix = match.group(1) if match else "calc"

    def write_ph_input(fname, tr2="1.0d-14", nq="2,2,2", search_sym=True, elph=False):
        nq1, nq2, nq3 = nq.split(",")
        sym_line = " search_sym=.false.,\n" if not search_sym else ""
        elph_lines = " fildvscf='dvscf',\n electron_phonon='interpolated',\n" if elph else ""
        with open(fname, "w") as f:
            f.write(
                f"Phonons\n&INPUTPH\n"
                f" tr2_ph={tr2},\n"
                f" prefix='{prefix}',\n"
                f" outdir='./tmp',\n"
                f" fildyn='{name}.dyn',\n"
                f" ldisp=.true.,\n"
                f"{elph_lines}"
                f"{sym_line}"
                f" nq1={nq1}, nq2={nq2}, nq3={nq3}\n"
                f"/\n"
            )

    def execute_ph_phase(is_elph_phase=False):
        ph_cores = get_scf_cores(scf_out, DEFAULT_CORES)
        print(f"      🧠 Erbe Kernanzahl von SCF, Starte mit {ph_cores} Core(s).")
        phonon_attempts = 0
        aainit_1core_done = False

        while phonon_attempts < 3:
            phonon_attempts += 1
            ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)
            
            if ph_res == "DONE": return "DONE"

            phase_name = "El-Ph" if is_elph_phase else "Stabilität"
            print(f"      ⚠️ Phonon Crash/OOM! (Phase, {phase_name})")
            crash_reason = analyze_crash_reason(ph_out)
            print_error_log(ph_out, "PHONON ERROR LOG")

            if crash_reason == "AAINIT_ERROR":
                if ph_cores > 1 and not aainit_1core_done:
                    print("      🔩 aainit-Fehler auf 2 Cores -> LÖSCHE _ph0 und wechsle auf 1 Core.")
                    ph_cores = 1
                    aainit_1core_done = True
                    for p in [os.path.join(work_dir, "tmp", "_ph0"),
                              os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                        if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                    if os.path.exists(ph_out): os.remove(ph_out)
                    phonon_attempts -= 1
                    continue
                else:
                    print("      🔩 aainit-Fehler unlösbar -> System-Komplexität zu hoch. Skippe.")
                    update_csv(name, f"SKIPPED (Phonon OOM, Phase {phase_name})")
                    git_sync(f"Phonon OOM, {name}")
                    return "CRASH"

            if crash_reason == "CORRUPT_FILE_ERROR":
                print("      🧨 Defekte Phonon-Datei -> Lösche _ph0 und starte Phonon-Phase neu...")
                for p in [os.path.join(work_dir, "tmp", "_ph0"),
                          os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                    if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                if os.path.exists(ph_out): os.remove(ph_out)
                phonon_attempts -= 1
                continue

            if crash_reason == "WF_COLLECT_ERROR":
                print("      🌊 Wellenfunktionen fehlen -> starte Collect-SCF (nstep=0)...")
                if run_cleanup_scf(scf_in, work_dir, ph_cores):
                    print("      ✅ Collect-SCF OK -> Phononen neu starten.")
                    if os.path.exists(ph_out): os.remove(ph_out)
                    phonon_attempts -= 1 
                    continue
                else:
                    print("      ❌ Collect-SCF fehlgeschlagen -> vollständiger SCF-Reset.")
                    tmp_save = os.path.join(work_dir, "tmp")
                    if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                    if os.path.exists(scf_out): os.remove(scf_out)
                    update_csv(name, "SCF_RESET (WFC Missing)")
                    return "SCF_RESET"

            if crash_reason == "XML_ERROR":
                print("      🧨 XML korrupt -> SCF-Reset.")
                tmp_save = os.path.join(work_dir, "tmp")
                if os.path.exists(tmp_save): shutil.rmtree(tmp_save, ignore_errors=True)
                if os.path.exists(scf_out):  os.remove(scf_out)
                update_csv(name, "SCF_RESET (XML Error)")
                return "SCF_RESET"

            if crash_reason in ["SYMMETRY_ERROR", "FFT_SYMMETRY_ERROR"]:
                print("      🧩 Symmetrie-Problem -> nosym injizieren + SCF-Reset.")
                source_in = os.path.join(INPUTS_DIR, f"{name}.in")
                if os.path.exists(source_in):
                    with open(source_in, 'r') as f: c = f.read()
                    if "nosym" not in c:
                        c = c.replace("&SYSTEM", "&SYSTEM\n nosym=.true.,")
                        with open(source_in, 'w') as f: f.write(c)
                if os.path.exists(work_dir): shutil.rmtree(work_dir, ignore_errors=True)
                update_csv(name, "SCF_RESET (Sym Error)")
                return "SCF_RESET"

            if crash_reason == "DAVCIO_ERROR" or is_recoverable_fragmentation_error(ph_out):
                print("      🤕 Fragmentierung -> 'Collect-Recovery'...")
                if run_cleanup_scf(scf_in, work_dir, int(DEFAULT_CORES)):
                    print("      👍 Recovery OK -> Phononen neu starten.")
                    if os.path.exists(ph_out): os.remove(ph_out)
                    continue
                print("      👎 Recovery fehlgeschlagen.")

            if crash_reason == "HARD":
                if os.path.exists(ph_out):
                    try:
                        ph_content_check = open(ph_out, errors='ignore').read()
                        if "bad line in namelist" in ph_content_check:
                            print("      📝 Namelist-Fehler -> schreibe ph.in komplett neu.")
                            nq_match = re.search(r"nq1\s*=\s*(\d+).*?nq2\s*=\s*(\d+).*?nq3\s*=\s*(\d+)", ph_content_check, re.DOTALL)
                            nq = current_nq
                            if nq_match: nq = f"{nq_match.group(1)},{nq_match.group(2)},{nq_match.group(3)}"
                            write_ph_input(ph_in, tr2="1.0d-14", nq=nq, elph=is_elph_phase)
                            if os.path.exists(ph_out): os.remove(ph_out)
                            phonon_attempts -= 1
                            continue
                    except: pass

            print(f"      🛡️ Phonon-Recovery, Versuch {phonon_attempts}/3")

            if phonon_attempts == 1:
                print("      📉 tr2_ph=1.0d-12")
                with open(ph_in, 'r') as f: c = f.read()
                c = re.sub(r"tr2_ph\s*=\s*[0-9\.dD\-]+", "tr2_ph=1.0d-12", c)
                with open(ph_in, 'w') as f: f.write(c)
                if os.path.exists(ph_out): os.remove(ph_out)
                continue

            elif phonon_attempts == 2:
                # WICHTIG: Das Grid darf bei el-ph nicht auf 1x1x1 reduziert werden!
                print(f"      🚨 NOTFALL-MODUS, Sym=OFF, tr2_ph=1.0d-10 (Grid bleibt {current_nq})")
                write_ph_input(ph_in, tr2="1.0d-10", nq=current_nq, search_sym=False, elph=is_elph_phase)
                for cleanup_path in [os.path.join(work_dir, "tmp", "_ph0"),
                                      os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")]:
                    if os.path.exists(cleanup_path): shutil.rmtree(cleanup_path, ignore_errors=True)
                if os.path.exists(ph_out): os.remove(ph_out)
                continue

        print("      ❌ Phononen endgültig fehlgeschlagen.")
        update_csv(name, "SKIPPED (Phonon Crash)")
        git_sync(f"Phonon Crash, {name}")
        return "CRASH"

    # -------------------------------------------------------------
    # INIT: Lese aktuelles Grid aus bestehender ph.in (falls existent)
    # -------------------------------------------------------------
    current_nq = "2,2,2"
    
    if os.path.exists(ph_in):
        with open(ph_in, 'r') as f:
            content = f.read()
            nq_match = re.search(r"nq1\s*=\s*(\d+).*?nq2\s*=\s*(\d+).*?nq3\s*=\s*(\d+)", content, re.DOTALL)
            if nq_match: current_nq = f"{nq_match.group(1)},{nq_match.group(2)},{nq_match.group(3)}"

    row_data = get_csv_full_info(name)
    already_stable = (row_data.get('Stabilität', '') == 'STABIL')

    # -------------------------------------------------------------
    # PHASE 1: Reine Stabilitätsanalyse (Kein dvscf, kein el-ph)
    # -------------------------------------------------------------
    if not already_stable:
        print(f"   🔍 PHASE 1, Stabilitätsanalyse für {name}...")
        
        write_ph_input(ph_in, nq=current_nq, elph=False)
        if os.path.exists(ph_out): os.remove(ph_out)
        
        phase1_res = execute_ph_phase(is_elph_phase=False)
        if phase1_res != "DONE": return phase1_res

        min_f, stab = "-", "Unbekannt"
        with open(ph_out, 'r') as f:
            content = f.read()
            freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
            if freqs:
                min_f = min(float(x) for x in freqs)
                stab  = "STABIL" if min_f > -0.05 else "INSTABIL"

        if stab == "INSTABIL":
            print(f"   🛑 Material ist INSTABIL (Min Freq, {min_f} THz). Überspringe El-Ph.")
            update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
            return "DONE"
            
        print(f"   ✅ Material ist STABIL (Min Freq, {min_f} THz). Gehe zu Phase 2...")
        update_csv(name, "Rechnet Phononen (El-Ph)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
    else:
        min_f = row_data.get('Min Freq (THz)', '-')

    # -------------------------------------------------------------
    # PHASE 2: Elektron-Phonon-Kopplung (Nur für stabile Materialien)
    # -------------------------------------------------------------
    print(f"   ⚛️ PHASE 2 Vorbereitung für {name}, Lösche alle alten Dateien...")
    tmp_path = os.path.join(work_dir, "tmp")
    
    ph0_path = os.path.join(tmp_path, "_ph0")
    if os.path.exists(ph0_path): shutil.rmtree(ph0_path, ignore_errors=True)
    
    chkpt_path = os.path.join(work_dir, "tmp_SAFE_PHONON_CHECKPOINT")
    if os.path.exists(chkpt_path): shutil.rmtree(chkpt_path, ignore_errors=True)
    
    for f in glob.glob(os.path.join(tmp_path, "*.a2Fsave*")): os.remove(f)
    for f in glob.glob(os.path.join(tmp_path, "*.dvscf*")): os.remove(f)

    if os.path.exists(ph_out): os.remove(ph_out)
    
    write_ph_input(ph_in, nq=current_nq, elph=True)
    print("   ⚛️ PHASE 2, Berechne Elektron-Phonon-Kopplung...")
    ph_result = execute_ph_phase(is_elph_phase=True)
    
    if ph_result == "DONE":
        try:
            with open(ph_out, 'r') as f:
                freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", f.read())
                if freqs:
                    min_f = min(float(x) for x in freqs)
        except: pass
        stab = "STABIL" if float(min_f) > -0.05 else "INSTABIL"
        update_csv(name, "Fertig (Phononen)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
        
    return ph_result

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

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        set_logic_app_state("Enabled")
        cleanup_system_memory()

        if not os.path.exists(TXT_LOG_FILE):
            with open(TXT_LOG_FILE, 'w', encoding='utf-8') as f:
                f.write(f"--- Init {datetime.now().strftime('%Y-%m-%d %H:%M')} ---\n")
            git_sync("📄 pipeline_output.txt initialisiert")

        truncate_log(TXT_LOG_FILE, max_size_mb=1.0)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {ts}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {ts}\n{'='*40}\n")

        if os.path.exists(SIGNAL_FILE):
            os.remove(SIGNAL_FILE)
            git_sync("🧹 rechnung_fertig.txt gelöscht (Neuer Start)")

        if not os.path.exists(INPUTS_DIR):
            os.makedirs(INPUTS_DIR)

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

            if "Metall" in last_status and stability in ["STABIL", "INSTABIL"] and row_data.get('Tc (K)', '-') != "-":
                cleanup_heavy_files(work_dir, name)
                print(f"⏩ Überspringe {name} (Bereits vollständig analysiert)")
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
                if not os.path.exists(work_dir):
                    os.makedirs(work_dir)
                print(f"\n💎 Job, {name}")
                scf_in = os.path.join(work_dir, "scf.in")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in):
                    shutil.copy(input_file, scf_in)

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
                            except:
                                pass

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
                    if oom_level >= 4:
                        current_cores = int(SAFE_CORES)

                    crash_counter = 0

                    while True:
                        apply_oom_settings(scf_in, oom_level)

                        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
                        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores)

                        if result == "DONE":
                            break

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
                            if reason == "NON_CONVERGED":
                                update_csv(name, "SKIPPED (Non-Conv)")
                                break
                            elif reason == "PSEUDO_ERROR":
                                update_csv(name, "SKIPPED (Pseudo Limit)")
                                print(f"      ❌ Skippe Job wegen inkompatiblem Pseudopotential.")
                                git_sync(f"Skipped {name}, Pseudo Error")
                                break
                            elif reason == "XML_ERROR":
                                print("      🧨 XML korrupt -> SCF-Reset.")
                                tmp_save = os.path.join(work_dir, "tmp")
                                if os.path.exists(tmp_save):
                                    shutil.rmtree(tmp_save, ignore_errors=True)
                                if os.path.exists(scf_out):
                                    os.remove(scf_out)
                                update_csv(name, "SCF_RESET (XML Error)")
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

                if result == "MAX_STEPS" or result == "OOM" or crash_counter >= 3 or analyze_crash_reason(scf_out) == "PSEUDO_ERROR":
                    continue
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
                        if match:
                            e_fermi = float(match.group(1))

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
                            if line.strip().startswith("#"):
                                continue
                            p = line.split()
                            if len(p) >= 2:
                                try:
                                    e, d = float(p[0]), float(p[1])
                                    if abs(e - e_fermi) < closest_diff:
                                        closest_diff = abs(e - e_fermi)
                                        dos_val = d
                                except:
                                    continue
                    is_metal = dos_val > DOS_THRESHOLD

                if not is_metal:
                    print(f"   🛑 Isolator (DOS={dos_val:.3f}).")
                    update_csv(name, "Fertig (Isolator)", e_fermi, round(dos_val, 4), "NEIN")
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")

                # Prüfen, ob die Phononen inklusive el-ph bereits vollständig vorliegen
                elph_files = (glob.glob(os.path.join(work_dir, "tmp", "*.a2Fsave*")) +
                              glob.glob(os.path.join(work_dir, "a2Fq2r.*")))
                phonon_already_done = (os.path.exists(ph_out) and "JOB DONE" in open(ph_out, errors='ignore').read())

                if phonon_already_done and not elph_files and stability == "STABIL":
                    print("      ⚠️ JOB DONE aber el-ph Dateien fehlen -> Neustart für El-Ph.")
                    try: os.remove(ph_out)
                    except: pass
                    phonon_already_done = False

                min_f, stab = "-", stability

                if not phonon_already_done:
                    ph_result = run_phonon_block(name, work_dir, scf_in, scf_out, ph_in, ph_out, e_fermi, dos_val)
                    if ph_result != "DONE":
                        continue
                    
                    row_data = get_csv_full_info(name)
                    min_f = row_data.get('Min Freq (THz)', '-')
                    stab = row_data.get('Stabilität', '-')

                if stab == "INSTABIL":
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (INSTABIL)")
                    continue

                if stab == "STABIL":
                    q2r_in = os.path.join(work_dir, "q2r.in")
                    q2r_out = os.path.join(work_dir, "q2r.out")
                    matdyn_in = os.path.join(work_dir, "matdyn.in")
                    matdyn_out = os.path.join(work_dir, "matdyn.out")

                    update_csv(name, "Rechnet El-Ph (Q2R)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print("   4️⃣  Q2R...")
                        with open(q2r_in, "w") as f:
                            f.write(f"&input\n fildyn='{name}.dyn',\n zasr='simple',\n flfrc='{name}.fc',\n la2F=.true.\n/\n")
                        with open(q2r_in, "r") as fi, open(q2r_out, "w") as fo:
                            subprocess.run([Q2R_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                        print(f"      ❌ Q2R fehlgeschlagen!")
                        update_csv(name, "ERROR (Q2R Crash)")
                        git_sync(f"Q2R Crash, {name}")
                        continue

                    update_csv(name, "Rechnet El-Ph (Matdyn)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)

                    if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print("   5️⃣  Matdyn...")
                        with open(matdyn_in, "w") as f:
                            f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n flfrq='{name}.freq',\n fildyn='{name}.dyn',\n dos=.true.,\n elph=.true.,\n fildos='{name}.phdos',\n nk1=10, nk2=10, nk3=10\n/\n")
                        with open(matdyn_in, "r") as fi, open(matdyn_out, "w") as fo:
                            subprocess.run([MATDYN_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

                    if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                        print(f"      ❌ Matdyn fehlgeschlagen!")
                        update_csv(name, "ERROR (Matdyn Crash)")
                        git_sync(f"Matdyn Crash, {name}")
                        continue

                    lam, wlog, tc = "-", "-", "-"
                    if os.path.exists(matdyn_out):
                        with open(matdyn_out, 'r', errors='ignore') as f:
                            mc = f.read()
                            if "JOB DONE" in mc:
                                ml = re.search(r"lambda\s*=\s*([0-9\.]+)", mc)
                                mw = re.search(r"omega_log\s*=\s*([0-9\.]+)", mc)
                                if ml and mw:
                                    lam = ml.group(1)
                                    wlog = mw.group(1)
                                    tc_v = berechne_tc(wlog, lam)
                                    if tc_v != "-":
                                        tc = round(tc_v, 3)

                    update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab, lam=lam, wlog=wlog, tc=tc)
                    cleanup_heavy_files(work_dir, name)
                    git_sync(f"Fertig, {name} (Tc={tc}K)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}, {job_err}")
                update_csv(name, f"ERROR (Python, {str(job_err)[:30]})")
                continue

        send_notification("🎉 Alle Jobs erledigt.")

        with open(SIGNAL_FILE, "w") as f:
            f.write(f"Status, Fertig\nTimestamp, {time.ctime()}")

        git_sync("🏁 Finaler Sync vor Shutdown (Erfolgreich)")

        set_logic_app_state("Disabled")

        print("🛑 Deallokiere VM über Azure CLI...")
        deallocate_vm()

        if os.name != 'nt':
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
                git_sync("🛑 Shutdown blockiert (SSH aktiv)")
            else:
                print("🛑 Fahre System herunter...")
                os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()})\n{e}\n{traceback.format_exc()}\n"
        with open(TXT_LOG_FILE, "a") as f:
            f.write(full_error)
        git_sync("🚨 Notfall Sync nach Skript-Absturz")
        send_notification(f"🚨 KRITISCHER FEHLER, {e} -> Shutdown.")

        set_logic_app_state("Disabled")
        print("🛑 Deallokiere VM über Azure CLI nach Crash...")
        deallocate_vm()
        if os.name != 'nt':
            if is_ssh_session_active():
                print("🛑 Shutdown blockiert (Aktive SSH-Sitzung erkannt!)")
            else:
                os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()