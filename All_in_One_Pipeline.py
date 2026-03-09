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
MAX_BFGS_STEPS = 100 
MAX_RETRIES_LEVEL = 3

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
# 2. HELFER & GIT & BACKUP
# =============================================================================
def backup_log_file():
    """Sichert das Logfile im laufenden Betrieb, BEVOR ein Crash passieren kann."""
    if os.path.exists(TXT_LOG_FILE):
        try:
            shutil.copy(TXT_LOG_FILE, BACKUP_LOG_FILE)
        except:
            pass

def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC, {message}"}
        requests.post(url, data=payload, timeout=10)
    except:
        pass

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"): return
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except:
        pass

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
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
            existing_fields = reader.fieldnames
            if existing_fields:
                for ef in existing_fields:
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
    except:
        return 1
    return max(1, count)

def berechne_tc(omega_log_K, lambda_ep, mu_star=0.13):
    try:
        lam = float(lambda_ep)
        wlog = float(omega_log_K)
        if lam <= 0: return 0.0
        vorfaktor = wlog / 1.20
        zaehler = -1.04 * (1.0 + lam)
        nenner = lam - mu_star * (1.0 + 0.62 * lam)
        if nenner <= 0: return 0.0
        return vorfaktor * math.exp(zaehler / nenner)
    except:
        return "-"

# =============================================================================
# 3. SMART LOGIC & VALIDATION & CRASH ANALYSE
# =============================================================================

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try:
                f.seek(-20000, 2)
            except OSError:
                f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore')
        
        if "JOB DONE" in lines: return "DONE"
        if "convergence NOT achieved" in lines: return "NON_CONVERGED"

        if "The maximum number of steps has been reached" in lines:
            return "RESTART_NEEDED"

        if "fatal error reading xml" in lines or "reading output_obj of xsd" in lines or "wrong number of occurrences" in lines:
            print("      🧨 XML-Struktur zerstört (Corruption).")
            return "XML_ERROR"

        if "not orthogonal" in lines and "D_S" in lines:
            print("      🧩 Symmetrie-Fehler erkannt (D_S not orthogonal).")
            return "SYMMETRY_ERROR"
            
        if "FFT grid incompatible with symmetry" in lines:
            print("      🧩 FFT-Gitter Inkompatibilität erkannt (Symmetrie Konflikt).")
            return "FFT_SYMMETRY_ERROR"
            
        if "error reading file" in lines and "xml" not in lines:
            print("      🤕 Fragmentierungsfehler erkannt (davcio).")
            return "DAVCIO_ERROR"

        ram_match = re.search(r"Estimated total dynamical RAM\s*>\s*([0-9\.]+)\s*GB", lines)
        if ram_match:
            if "Self-consistent Calculation" not in lines and "iteration #" not in lines:
                return "LIKELY_OOM"

        if "iteration #" in lines or "diagonalization" in lines:
            error_keywords = ["Error", "error", "Mpi_Abort", "segmentation fault", "stopping", "fatal"]
            has_error_msg = any(key in lines for key in error_keywords)
            if not has_error_msg:
                return "LIKELY_OOM"

        error_keywords = ["Error", "error", "Mpi_Abort", "segmentation fault", "stopping", "diagonalization failed"]
        for key in error_keywords:
            if key in lines: return "HARD"
            
        return "SOFT"
    except: return "HARD"

def is_xml_valid(xml_path):
    if not os.path.exists(xml_path): return False
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
    
    content = re.sub(r"nq1\s*=\s*\d+", "nq1=1", content)
    content = re.sub(r"nq2\s*=\s*\d+", "nq2=1", content)
    content = re.sub(r"nq3\s*=\s*\d+", "nq3=1", content)

    with open(input_file, 'w') as f: f.write(content)
    print(f"      🛡️ Symmetrien deaktiviert & Grid auf 1x1x1 reduziert.")

# --- PERSISTENZ-LOGIK ---
def detect_oom_level(input_file):
    if not os.path.exists(input_file): return 0
    with open(input_file, 'r', errors='ignore') as f: content = f.read()
    if "mixing_ndim = 2" in content or "mixing_ndim=2" in content: return 4
    if "mixing_ndim = 3" in content or "mixing_ndim=3" in content: return 3
    if "disk_io='low'" in content or 'disk_io="low"' in content: return 2
    if "diagonalization='cg'" in content or 'diagonalization="cg"' in content: return 1
    return 0

def apply_oom_settings(input_file, level, force_cg=False):
    with open(input_file, 'r') as f: content = f.read()
    diag = 'david'
    mix = 6  
    disk = None 
    msg = "Standard (david, mix=6)"

    if level >= 1 or force_cg: 
        diag = 'cg'
        mix = 4
        msg = "Stufe 1 oder CG erzwungen (cg, mix=4)"
    if level >= 2: 
        disk = 'low'
        msg = "Stufe 2 (cg, mix=4, disk_io='low')"
    if level >= 3: 
        mix = 3
        msg = "Stufe 3 (cg, mix=3, disk_io='low')"
    if level >= 4: 
        mix = 2
        msg = "Stufe 4 (cg, mix=2, disk_io='low', 1 Core)"

    print(f"      📉 Setze RAM/Konvergenz-Strategie, {msg}")

    if "diagonalization" in content:
        content = re.sub(r"diagonalization\s*=\s*['\"].*['\"]", f"diagonalization='{diag}'", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n diagonalization='{diag}',")

    if "mixing_ndim" in content:
        content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {mix}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {mix},")

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

    # --- VERBESSERTES METALL/SLOSHING-MANAGEMENT ---
    if "nstep" in content:
        content = re.sub(r"nstep\s*=\s*\d+", "nstep = 100", content)
    else:
        content = content.replace("&CONTROL", "&CONTROL\n nstep = 100,")

    if "mixing_mode" not in content:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_mode = 'local-TF',")
        
    if "diago_david_ndim" not in content:
         content = content.replace("&ELECTRONS", "&ELECTRONS\n diago_david_ndim = 2,")
    else:
         content = re.sub(r"diago_david_ndim\s*=\s*\d+", "diago_david_ndim = 2", content)

    target_beta = 0.4
    target_mix_ndim = 6
    
    if iteration_count >= 30: target_beta = 0.2
    if iteration_count >= 60: 
        target_beta = 0.1
        target_mix_ndim = 3
    if iteration_count >= 90: 
        target_beta = 0.05   
        target_mix_ndim = 2

    if "mixing_beta" in content:
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_beta = {target_beta},")
        
    if "mixing_ndim" in content:
        mix_match = re.search(r"mixing_ndim\s*=\s*(\d+)", content)
        if mix_match and int(mix_match.group(1)) > target_mix_ndim:
             content = re.sub(r"mixing_ndim\s*=\s*\d+", f"mixing_ndim = {target_mix_ndim}", content)
    else:
        content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = {target_mix_ndim},")

    if "electron_maxstep" in content:
        content = re.sub(r"electron_maxstep\s*=\s*\d+", "electron_maxstep = 300", content)
    else:
        content = content.replace("&ELECTRONS", "&ELECTRONS\n electron_maxstep = 300,")

    if iteration_count >= 60:
        if "smearing" in content:
            content = re.sub(r"smearing\s*=\s*['\"][a-zA-Z\-]+['\"]", "smearing='m-v'", content)
        else:
            content = content.replace("&SYSTEM", "&SYSTEM\n smearing='m-v',")

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

# --- ROBUSTE PWSCF WRAPPER ---
def run_monitored_pw(input_file, output_file, cwd, active_cores, force_cg=False):
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
            except Exception as e:
                print(f"      ❌ Fehler beim Laden des Checkpoints, {e}")
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
        
        # LOG BACKUP BEVOR ES LOSGEHT
        backup_log_file()
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE]
            print(f"      ⚙️ Starte PWSCF ({mode}, {active_cores} Cores)...")
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
            
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
                                print("      ☁️ Trigger Git Sync (wegen Checkpoint)...")
                                git_sync("Checkpoint & Log Update")
                                backup_log_file() # Laufendes Backup sichern
                                last_git_sync = time.time() 
                            except Exception as e:
                                print(f"      ⚠️ Checkpoint fail, {e}")

                    if time.time() - last_git_sync > 3600:
                        print("      ❤️ Git Heartbeat...")
                        git_sync("Log Update (Heartbeat)")
                        backup_log_file() # Laufendes Backup sichern
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
                    
                    if cur_iter > 30: fix_input_file(input_file, cur_iter)

            except:
                process.kill()
                return "CRASH"
            
        if process.returncode == -9:
            print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
            return "OOM"

        reason = analyze_crash_reason(output_file)
        
        if reason == "DONE":
            if process.returncode != 0:
                print("      ⚠️ MPI-Fehlalarm ignoriert (JOB DONE gefunden).")
            return "DONE"
            
        elif reason == "RESTART_NEEDED":
            print("      🔄 Reguläres nstep-Limit erreicht. Neustart für weitere Optimierung nötig.")
            return "RESTART_NEEDED"
            
        elif reason == "LIKELY_OOM":
            print("      💀 Logfile endet abrupt (Silent Death) -> OOM.")
            return "OOM"
            
        return "CRASH"

# --- ROBUSTE PHONON WRAPPER ---
def run_monitored_ph(input_file, output_file, cwd, active_cores):
    last_git_sync = time.time()
    last_checkpoint_time = time.time()
    tmp_dir = os.path.join(cwd, "tmp")
    ph0_dir = os.path.join(tmp_dir, "_ph0")
    checkpoint_dir = os.path.join(cwd, "tmp_SAFE_PHONON_CHECKPOINT")

    with open(input_file, 'r') as f: content = f.read()

    # Phonon Checkpoint Recovery Logik
    if os.path.exists(output_file) and not os.path.exists(ph0_dir) and os.path.exists(checkpoint_dir):
        print("      🛡️ _ph0 Ordner fehlt/defekt! Hole Phonon Safe-Checkpoint...")
        try:
            shutil.copytree(checkpoint_dir, ph0_dir)
            print("      ✅ Phonon Checkpoint erfolgreich geladen!")
        except Exception as e:
            print(f"      ❌ Fehler beim Laden des Phonon Checkpoints, {e}")

    if os.path.exists(output_file) and os.path.exists(ph0_dir):
        if "recover" not in content:
            content = content.replace("&INPUTPH", "&INPUTPH\n recover=.true.,")
    else:
        if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir, ignore_errors=True)
    
    run_input = input_file + ".run"
    with open(run_input, 'w') as f: f.write(content)
    
    file_mode = 'a' if "recover=.true." in content else 'w'

    # LOG BACKUP BEVOR ES LOSGEHT
    backup_log_file()

    with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
        cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
        print(f"      ⚙️ Starte PHONONEN (Cores, {active_cores})...")
        process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        
        try:
            while process.poll() is None:
                time.sleep(5)
                current_time = time.time()
                
                if current_time - last_git_sync > 1800:
                    print("      ❤️ Git Heartbeat (Phonon)...")
                    git_sync("Log Update (Phonon Running)")
                    backup_log_file() # Laufendes Backup sichern
                    last_git_sync = current_time

                # Phonon-Checkpointing alle 20 Minuten
                if current_time - last_checkpoint_time > 1200:
                    if os.path.exists(ph0_dir):
                        print("      💾 Erstelle Phonon-Checkpoint...")
                        try:
                            if os.path.exists(checkpoint_dir): shutil.rmtree(checkpoint_dir)
                            shutil.copytree(ph0_dir, checkpoint_dir)
                            last_checkpoint_time = current_time
                            backup_log_file() # Laufendes Backup sichern
                            print("      ✅ Phonon-Checkpoint gesichert.")
                        except Exception as e:
                            print(f"      ⚠️ Phonon-Checkpoint fail, {e}")

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
        
    if process.returncode == -9:
        print("      💀 Prozess wurde vom OS getötet (Exit -9 -> Wahrscheinlich OOM).")
        return "OOM"

    try:
        with open(output_file, 'r', errors='ignore') as f:
            if "JOB DONE" in f.read():
                if process.returncode != 0:
                    print("      ⚠️ MPI-Fehlalarm ignoriert (JOB DONE gefunden).")
                return "DONE"
    except:
        pass
    
    return "CRASH"

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        set_logic_app_state("Enabled")
        with open(TXT_LOG_FILE, "a") as f:
            f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        print(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        
        if os.path.exists(SIGNAL_FILE): 
            os.remove(SIGNAL_FILE)
            git_sync("🧹 rechnung_fertig.txt gelöscht (Neuer Start)")
            
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        send_notification(f"Start, {len(input_files)} Jobs.")
        git_sync("🚀 Start")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            scf_out = os.path.join(work_dir, "scf.out")
            
            row_data = get_csv_full_info(name)
            last_status = row_data.get('Status', 'NEW')
            stability = row_data.get('Stabilität', '-')
            tc_status = row_data.get('Tc (K)', '-')

            if "SKIPPED" in last_status:
                print(f"⏩ Überspringe {name} (Status, {last_status})")
                continue
            
            if "Isolator" in last_status:
                print(f"⏩ Überspringe {name} (Ist ein Isolator)")
                continue

            if "Metall" in last_status and stability in ["STABIL", "INSTABIL"] and tc_status != "-":
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
                if not os.path.exists(work_dir): os.makedirs(work_dir)
                print(f"\n💎 Job, {name}")
                scf_in = os.path.join(work_dir, "scf.in")
                dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
                ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

                if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

                if not (os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read()):
                    update_csv(name, "Rechnet SCF...")
                    
                    file_level = detect_oom_level(scf_in)
                    start_crash_reason = analyze_crash_reason(scf_out)
                    
                    if start_crash_reason == "LIKELY_OOM":
                        # Bezieht die Crash-Historie aus dem aktuellen UND aus dem Backup-Log
                        attempts = count_job_attempts(TXT_LOG_FILE, name)
                        if attempts == 1 and os.path.exists(BACKUP_LOG_FILE):
                            attempts += count_job_attempts(BACKUP_LOG_FILE, name)

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
                    if oom_level >= 4: current_cores = int(SAFE_CORES)
                    
                    crash_counter = 0  
                    oom_counter = 0  
                    
                    while True:
                        force_cg = False
                        if os.path.exists(scf_out):
                            try:
                                with open(scf_out, 'r', errors='ignore') as f_out_check:
                                    if "eigenvalues not converged" in f_out_check.read():
                                        force_cg = True
                                        print("      ⚠️ Konvergenz-Probleme erkannt, erzwinge CG-Diagonalisierung.")
                            except: pass

                        apply_oom_settings(scf_in, oom_level, force_cg)
                        
                        print(f"   1️⃣  SCF ({current_cores} Cores, OOM-Lvl {oom_level})")
                        result = run_monitored_pw(scf_in, scf_out, work_dir, current_cores, force_cg)
                        
                        if result == "DONE": break 
                        
                        elif result == "MAX_STEPS":
                            update_csv(name, "SKIPPED (Max BFGS Steps)")
                            git_sync(f"Skipped {name}, >{MAX_BFGS_STEPS} BFGS Steps")
                            break
                            
                        elif result == "RESTART_NEEDED":
                            update_csv(name, "Rechnet SCF (Fortsetzung)...")
                            print("      🔄 Reguläres nstep-Limit erreicht. Setze Geometrie-Optimierung fort...")
                            continue

                        elif result == "OOM":
                            oom_counter += 1
                            if oom_counter < 3:
                                print(f"      ⚠️ OOM Verdacht. Versuch {oom_counter}/3 auf Level {oom_level}...")
                                update_csv(name, f"Retrying (OOM Wait {oom_counter}/3)")
                                time.sleep(2)
                                continue
                                
                            oom_level += 1
                            oom_counter = 0
                            crash_counter = 0
                            print(f"      ⚠️ OOM Limit erreicht. Eskaliere zu Level {oom_level}...")
                            
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

                    if result == "MAX_STEPS" or result == "OOM" or crash_counter >= 3: continue 
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
                    git_sync(f"Fertig, {name} (Isolator)")
                    continue

                print(f"   ⚡ Metall (DOS={dos_val:.3f}). Berechne Phononen...")
                update_csv(name, "Rechnet Phononen...", e_fermi, round(dos_val, 4), "JA")
                
                if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                    if not os.path.exists(ph_in):
                        with open(ph_in, "w") as f: 
                            f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., elph=.true., nq1=2, nq2=2, nq3=2 /\n")
                    
                    ph_cores = int(DEFAULT_CORES)
                    ph_attempts_hist = count_job_attempts(TXT_LOG_FILE, name)
                    if os.path.exists(BACKUP_LOG_FILE) and ph_attempts_hist == 1:
                         ph_attempts_hist += count_job_attempts(BACKUP_LOG_FILE, name)
                    if ph_attempts_hist > 1: ph_cores = 1

                    phonon_attempts = 0
                    phonon_success = False
                    
                    while phonon_attempts < 3:
                        phonon_attempts += 1
                        ph_res = run_monitored_ph(ph_in, ph_out, work_dir, ph_cores)
                        
                        if ph_res == "DONE":
                            phonon_success = True
                            break
                            
                        print("      ⚠️ Crash/OOM!")
                        crash_reason = analyze_crash_reason(ph_out)
                        
                        if crash_reason == "XML_ERROR":
                            print("      🧨 FATAL, XML korrupt. Lösche .save und erzwinge SCF-Neustart im nächsten Durchlauf.")
                            tmp_save_path = os.path.join(work_dir, "tmp")
                            if os.path.exists(tmp_save_path): shutil.rmtree(tmp_save_path, ignore_errors=True)
                            if os.path.exists(scf_out): os.remove(scf_out)
                            update_csv(name, "SCF_RESET (XML Error)")
                            break 

                        if crash_reason in ["SYMMETRY_ERROR", "FFT_SYMMETRY_ERROR"]:
                            print("      🧩 Symmetrie-Problem erkannt. Lösche RUN Ordner und injiziere nosym=.true.")
                            source_in = os.path.join(INPUTS_DIR, f"{name}.in")
                            if os.path.exists(source_in):
                                with open(source_in, 'r') as f: c = f.read()
                                if "nosym" not in c:
                                    c = c.replace("&SYSTEM", "&SYSTEM\n nosym=.true.,")
                                    with open(source_in, 'w') as f: f.write(c)
                            if os.path.exists(work_dir): shutil.rmtree(work_dir, ignore_errors=True)
                            update_csv(name, "SCF_RESET (Sym Error)")
                            break

                        if crash_reason == "DAVCIO_ERROR" or is_recoverable_fragmentation_error(ph_out):
                            print("      🤕 Diagnose, Fragmentierung erkannt. Starte 'Collect-Recovery'...")
                            if run_cleanup_scf(scf_in, work_dir, int(DEFAULT_CORES)):
                                print("      👍 Recovery erfolgreich. Starte Phononen neu...")
                                if os.path.exists(ph_out): os.remove(ph_out)
                                continue
                            else:
                                print("      👎 Recovery fehlgeschlagen.")
                        
                        print(f"      🛡️ Phonon-Recovery, Versuch {phonon_attempts}/3")
                        
                        if phonon_attempts == 1:
                            print("      📉 Lockere Konvergenzgrenze auf tr2_ph=1.0d-12")
                            with open(ph_in, 'r') as f: c = f.read()
                            c = re.sub(r"tr2_ph\s*=\s*[0-9\.dD\-]+", "tr2_ph=1.0d-12", c)
                            with open(ph_in, 'w') as f: f.write(c)
                            
                            if os.path.exists(ph_out): os.remove(ph_out) 
                            continue
                            
                        elif phonon_attempts == 2:
                            print("      🚨 Aktiviere NOTFALL-MODUS, Grid=1x1x1, Sym=OFF, 1 Core, tr2_ph=1.0d-10")
                            disable_symmetries_and_reduce_grid(ph_in)
                            with open(ph_in, 'r') as f: c = f.read()
                            c = re.sub(r"tr2_ph\s*=\s*[0-9\.dD\-]+", "tr2_ph=1.0d-10", c)
                            with open(ph_in, 'w') as f: f.write(c)
                            
                            ph_cores = 1
                            tmp_path = os.path.join(work_dir, "tmp")
                            ph0_path = os.path.join(tmp_path, "_ph0")
                            
                            if os.path.exists(ph0_path):
                                try: shutil.rmtree(ph0_path, ignore_errors=True)
                                except: pass
                            
                            if os.path.exists(ph_out):
                                try: os.remove(ph_out)
                                except: pass
                            continue
                        
                    if not phonon_success:
                         print("      ❌ Phononen endgültig fehlgeschlagen.")
                         update_csv(name, "SKIPPED (Phonon Crash)") 
                         git_sync(f"Phonon Crash, {name}")
                         continue

                min_f, stab = "-", "Unbekannt"
                if os.path.exists(ph_out):
                      with open(ph_out, 'r') as f:
                          content = f.read()
                          if "JOB DONE" in content:
                              freqs = re.findall(r"freq\s+\(\s*\d+\)\s+=\s+([0-9\.\-]+)\s+\[THz\]", content)
                              if freqs:
                                  min_f = min([float(f) for f in freqs])
                                  stab = "STABIL" if min_f > -0.05 else "INSTABIL"

                if stab == "STABIL":
                    update_csv(name, "Rechnet El-Ph (Q2R)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    
                    q2r_in = os.path.join(work_dir, "q2r.in")
                    q2r_out = os.path.join(work_dir, "q2r.out")
                    if not os.path.exists(q2r_out) or "JOB DONE" not in open(q2r_out, errors='ignore').read():
                        print("   4️⃣  Q2R Berechnung...")
                        with open(q2r_in, "w") as f:
                            f.write(f"&input\n fildyn='{name}.dyn',\n zasr='simple',\n flfrc='{name}.fc'\n/\n")
                        with open(q2r_in, "r") as f_in, open(q2r_out, "w") as f_out:
                            subprocess.run([Q2R_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)
                            
                    update_csv(name, "Rechnet El-Ph (Matdyn)...", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    
                    matdyn_in = os.path.join(work_dir, "matdyn.in")
                    matdyn_out = os.path.join(work_dir, "matdyn.out")
                    if not os.path.exists(matdyn_out) or "JOB DONE" not in open(matdyn_out, errors='ignore').read():
                        print("   5️⃣  Matdyn Berechnung...")
                        with open(matdyn_in, "w") as f:
                            f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n flfrq='{name}.freq',\n fildyn='{name}.dyn',\n dos=.true.,\n elph=.true.,\n fildos='{name}.phdos',\n nk1=10, nk2=10, nk3=10\n/\n")
                        with open(matdyn_in, "r") as f_in, open(matdyn_out, "w") as f_out:
                            subprocess.run([MATDYN_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=work_dir)
                            
                    lam, wlog, tc = "-", "-", "-"
                    if os.path.exists(matdyn_out):
                        with open(matdyn_out, 'r', errors='ignore') as f:
                            content = f.read()
                            if "JOB DONE" in content:
                                match_lam = re.search(r"lambda\s*=\s*([0-9\.]+)", content)
                                match_wlog = re.search(r"omega_log\s*=\s*([0-9\.]+)", content)
                                if match_lam and match_wlog:
                                    lam = match_lam.group(1)
                                    wlog = match_wlog.group(1)
                                    tc_val = berechne_tc(wlog, lam)
                                    if tc_val != "-":
                                        tc = round(tc_val, 3)
                                        
                    update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab, lam=lam, wlog=wlog, tc=tc)
                    git_sync(f"Fertig, {name} (Tc={tc}K)")
                else:
                    update_csv(name, "Fertig (Metall)", e_fermi, round(dos_val, 4), "JA", min_f=min_f, stab=stab)
                    git_sync(f"Fertig, {name} (Metall)")

            except Exception as job_err:
                print(f"🚨 Fehler bei Job {name}, {job_err}")
                update_csv(name, f"ERROR (Python, {str(job_err)[:30]})")
                continue 
            
        send_notification("🎉 Alle Jobs erledigt.")
        set_logic_app_state("Disabled") 
        
        with open(SIGNAL_FILE, "w") as f: f.write(f"Status, Fertig\nTimestamp, {time.ctime()}")
        git_sync("🏁 Pipeline vollständig beendet (rechnung_fertig.txt erstellt)")
        
        if os.name != 'nt': os.system("sudo shutdown -h now")

    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()}),\n{e}\n{traceback.format_exc()}\n"
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        send_notification(f"🚨 KRITISCHER FEHLER, {e} -> Shutdown.")
        set_logic_app_state("Disabled")
        if os.name != 'nt': os.system("sudo shutdown -h now")
        sys.exit()

if __name__ == "__main__":
    main()