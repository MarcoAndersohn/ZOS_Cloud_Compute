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
TELEGRAM_TOKEN = open("/home/marco/.telegram_token").read().strip()
TELEGRAM_CHAT_ID = "711461437"

LOGIC_APP_NAME = "AutoRestart-Supraleiter"
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"
DOS_THRESHOLD = 0.05

DEFAULT_CORES = "4"
SAFE_CORES = "2"
MEMORY_LIMIT_PERCENT = 92.0
MAX_BFGS_STEPS = 100 
MAX_RETRIES_LEVEL = 3

FORCE_RETRY_LIST = []

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")
TXT_LOG_FILE = os.path.join(WORK_DIR, "pipeline_output.txt")

PW_EXE = "pw.x"
PH_EXE = "ph.x"
DOS_EXE = "dos.x"
Q2R_EXE = "q2r.x"
MATDYN_EXE = "matdyn.x"

# =============================================================================
# 2. HELFER & GIT
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ HPC {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def set_logic_app_state(state="Enabled"):
    if not shutil.which("az"): return
    try:
        subprocess.run(["az", "logic", "workflow", "set-state", "--resource-group", RESOURCE_GROUP, "--name", LOGIC_APP_NAME, "--state", state], capture_output=True, timeout=30)
    except: pass

def kill_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True): child.kill()
        parent.kill()
    except: pass

def git_sync(message):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "config", "credential.helper", "store"], cwd=WORK_DIR, env=env, timeout=10)
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, env=env, timeout=30)
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
    except: pass

def print_error_tail(output_file, lines=50):
    if not os.path.exists(output_file): return
    try:
        with open(output_file, 'r', errors='ignore') as f:
            content = f.readlines()
            print(f"\n--- Letzte {lines} Zeilen von {os.path.basename(output_file)} ---\n{''.join(content[-lines:])}\n-----------------------------------------\n")
    except: pass

def update_csv(name, status, e_fermi="-", dos_val="-", is_metal="-", min_f="-", stab="-", lam="-", wlog="-", tc="-"):
    fieldnames = ['Name', 'Status', 'Fermi Energie (eV)', 'DOS @ Fermi', 'Metall?', 'Min Freq (THz)', 'Stabilität', 'Lambda', 'Omega_log (K)', 'Tc (K)', 'Timestamp']
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
            if lam != "-": row['Lambda'] = str(lam)
            if wlog != "-": row['Omega_log (K)'] = str(wlog)
            if tc != "-": row['Tc (K)'] = str(tc)
            found = True; break
    if not found:
        new_row = {'Name': name, 'Status': status, 'Fermi Energie (eV)': str(e_fermi), 'DOS @ Fermi': str(dos_val), 'Metall?': str(is_metal), 'Min Freq (THz)': str(min_f), 'Stabilität': str(stab), 'Timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")}
        rows.append(new_row)
    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader(); writer.writerows(rows)

def get_csv_full_info(name):
    if not os.path.exists(CSV_FILE): return {}
    with open(CSV_FILE, 'r') as f:
        for row in csv.DictReader(f):
            if row['Name'] == name: return row
    return {}

def count_job_attempts(log_file, job_name):
    if not os.path.exists(log_file): return 1
    try:
        with open(log_file, 'rb') as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 50000), 0)
            lines = f.read().decode('utf-8', errors='ignore').splitlines()
        count = sum(1 for line in lines if f"💎 Job {job_name}" in line)
        return max(1, count)
    except: return 1

# =============================================================================
# 3. KERN-LOGIK
# =============================================================================
def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            f.seek(-20000, 2) if os.path.getsize(output_file) > 20000 else f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore').lower()
        if "job done" in lines: return "DONE"
        if any(x in lines for x in ["error", "mpi_abort", "segmentation fault", "fatal"]): return "HARD"
        return "SOFT"
    except: return "HARD"

def run_monitored_pw(input_file, output_file, cwd, active_cores):
    fix_input_file(input_file, 0)
    cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PW_EXE, "-ndiag", "1"]
    with open(input_file, 'r') as f_in, open(output_file, 'w') as f_out:
        proc = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        while proc.poll() is None:
            time.sleep(5)
            if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                kill_process_tree(proc.pid); return "OOM"
        if proc.returncode != 0: 
            print_error_tail(output_file)
            return "CRASH"
        return "DONE"

def run_monitored_ph(input_file, output_file, cwd, active_cores):
    cmd = ["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE]
    with open(input_file, 'r') as f_in, open(output_file, 'w') as f_out:
        proc = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        while proc.poll() is None:
            time.sleep(5)
            if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                kill_process_tree(proc.pid); return "OOM"
        if proc.returncode != 0: 
            print_error_tail(output_file)
            return "CRASH"
        return "DONE"

def fix_input_file(input_file, iteration_count=0):
    with open(input_file, 'r') as f: content = f.read()
    content = re.sub(r"pseudo_dir\s*=\s*['\"].*['\"]", f"pseudo_dir='{PSEUDO_DIR}'", content)
    
    # NEU: la2f fuer QE 7.4 in den &ELECTRONS Block injizieren
    if "la2f" not in content.lower():
        content = content.replace("&ELECTRONS", "&ELECTRONS\n la2f=.true.,")
        
    with open(input_file, 'w') as f: f.write(content)

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        input_files = sorted(glob.glob(os.path.join(INPUTS_DIR, "*.in")))
        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            os.makedirs(work_dir, exist_ok=True)
            
            scf_in, scf_out = os.path.join(work_dir, "scf.in"), os.path.join(work_dir, "scf.out")
            ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")
            if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

            print(f"\n💎 Job {name}")
            if run_monitored_pw(scf_in, scf_out, work_dir, DEFAULT_CORES) != "DONE": continue
            
            # PHONON + ELPH (Option C, 7.4 kompatibel)
            if not os.path.exists(ph_out):
                with open(ph_in, "w") as f:
                    f.write(f"&INPUTPH\n tr2_ph=1.0d-14, prefix='{name}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., electron_phonon='interpolated', fildvscf='dvscf', nq1=2, nq2=2, nq3=2 /\n")
                if run_monitored_ph(ph_in, ph_out, work_dir, DEFAULT_CORES) != "DONE": continue
            
            # Matdyn/Lambda Logik folgt hier...
    except Exception as e:
        traceback.print_exc()
        os.system("sudo shutdown -h now")

if __name__ == "__main__":
    main()