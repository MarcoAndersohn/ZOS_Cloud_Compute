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
try:
    TELEGRAM_TOKEN = open("/home/marco/.telegram_token").read().strip()
except:
    TELEGRAM_TOKEN = ""
    
TELEGRAM_CHAT_ID = "711461437"

LOGIC_APP_NAME = "AutoRestart-Supraleiter"
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"
DOS_THRESHOLD = 0.05

# Exakt wie von dir vorgegeben (Keine Drosselung):
DEFAULT_CORES = "4"
SAFE_CORES = "2"
MEMORY_LIMIT_PERCENT = 92.0
MAX_BFGS_STEPS = 100 
MAX_RETRIES_LEVEL = 5

FORCE_RETRY_LIST = []

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
# 2. DEINE BEWÄHRTEN HELFER & GIT
# =============================================================================
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

def kill_zombie_processes():
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] in ['mpirun', 'ph.x', 'pw.x']:
                proc.kill()
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
    try:
        subprocess.run(["git", "config", "credential.helper", "store"], cwd=WORK_DIR, env=env, timeout=10)
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, env=env, timeout=30)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, capture_output=True, env=env, timeout=30)
        subprocess.run(["git", "pull", "origin", "main", "--strategy-option=ours", "--no-rebase"], cwd=WORK_DIR, env=env, timeout=60, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=WORK_DIR, env=env, timeout=60)
    except Exception as e:
        print(f"⚠️ Git Fehler, {e}")

def print_error_tail(log_file, lines=50):
    if not os.path.exists(log_file): return
    try:
        with open(log_file, 'r', errors='ignore') as f:
            tail = f.readlines()[-lines:]
        err = f"\n      --- LETZTE {lines} ZEILEN VON {os.path.basename(log_file)} ---\n" + "".join([f"      {l.rstrip()}\n" for l in tail])
        print(err)
        with open(TXT_LOG_FILE, 'a') as f_out:
            f_out.write(err)
    except: pass

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
    except: return "-"

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

# =============================================================================
# 3. CRASH ANALYSE & PHONON ENGINE
# =============================================================================
def is_xml_valid(xml_path):
    if not os.path.exists(xml_path): return False
    try:
        with open(xml_path, 'rb') as f:
            f.seek(max(0, os.path.getsize(xml_path) - 1000))
            tail = f.read().decode('utf-8', errors='ignore')
        return "</qes:espresso>" in tail or "</qes:data-file-schema>" in tail
    except: return False

def manage_rolling_checkpoints(work_dir):
    tmp_dir = os.path.join(work_dir, "tmp")
    bkp1 = os.path.join(work_dir, "tmp_SAFE_1")
    bkp2 = os.path.join(work_dir, "tmp_SAFE_2")
    if not os.path.exists(tmp_dir): return
    if os.path.exists(bkp1):
        if os.path.exists(bkp2): shutil.rmtree(bkp2, ignore_errors=True)
        try: shutil.move(bkp1, bkp2)
        except: pass
    try: shutil.copytree(tmp_dir, bkp1)
    except: pass

def restore_rolling_checkpoint(work_dir, prefix):
    tmp_dir = os.path.join(work_dir, "tmp")
    for bkp in [os.path.join(work_dir, "tmp_SAFE_1"), os.path.join(work_dir, "tmp_SAFE_2")]:
        if os.path.exists(bkp):
            msg = f"      🔄 Lade Checkpoint aus {os.path.basename(bkp)}..."
            print(msg)
            with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg + "\n")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                shutil.copytree(bkp, tmp_dir)
                if is_xml_valid(os.path.join(tmp_dir, f"{prefix}.save", "data-file-schema.xml")):
                    return True
            except: pass
    return False

def analyze_crash_reason(output_file):
    if not os.path.exists(output_file): return "NONE"
    try:
        with open(output_file, 'rb') as f:
            try: f.seek(-20000, 2)
            except OSError: f.seek(0)
            lines = f.read().decode('utf-8', errors='ignore').lower()
        
        if "job done" in lines: return "DONE"
        if "convergence not achieved" in lines: return "NON_CONVERGED"
        if "fatal error reading xml" in lines or "tag root not found" in lines or "xmltools.f90" in lines: return "XML_ERROR"
        if "not orthogonal" in lines and "d_s" in lines: return "SYMMETRY_ERROR"
        if "mx dimension too small" in lines: return "PSEUDO_ERROR"
        if "i/o past end of record" in lines or "end of file" in lines: return "ELPH_CORRUPT"

        error_keywords = ["error", "mpi_abort", "segmentation fault", "stopping", "fatal", "diagonalization failed"]
        has_error_msg = any(key in lines for key in error_keywords)
        if has_error_msg: return "HARD"

        ram_match = re.search(r"estimated total dynamical ram\s*>\s*([0-9\.]+)\s*(mb|gb)", lines)
        if ram_match and "self-consistent calculation" not in lines and "iteration #" not in lines: return "LIKELY_OOM"
        if "iteration #" in lines or "diagonalization" in lines:
            if not has_error_msg: return "LIKELY_OOM"
        
        return "SOFT"
    except: return "HARD"

def run_monitored_ph(input_file, output_file, cwd, active_cores):
    kill_zombie_processes()
    last_git_sync = time.time()
    last_cp = time.time()

    with open(input_file, 'r') as f: content = f.read()
    ph0_dir = os.path.join(cwd, "tmp", "_ph0")
    rec_mode = "recover=.true." if os.path.exists(ph0_dir) else "recover=.false."

    if "recover=" in content.lower():
        content = re.sub(r"recover\s*=\s*\.[a-zA-Z]+\.", rec_mode, content, flags=re.IGNORECASE)
    else:
        content = content.replace("&INPUTPH", f"&INPUTPH\n {rec_mode},\n")
        
    with open(input_file+".run", 'w') as f: f.write(content)
    file_mode = 'a' if ("recover=.true." in content.lower()) else 'w'

    with open(input_file+".run", 'r') as f_in, open(output_file, file_mode) as f_out:
        msg = f"      ⚙️ Starte PHONONEN ({active_cores} Cores, {rec_mode})..."
        print(msg)
        with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg + "\n")
        
        process = subprocess.Popen(["mpirun", "--oversubscribe", "-np", str(active_cores), PH_EXE], stdin=f_in, stdout=f_out, stderr=subprocess.STDOUT, cwd=cwd)
        
        try:
            while process.poll() is None:
                time.sleep(2)
                if time.time() - last_cp > 1800:
                    manage_rolling_checkpoints(cwd)
                    last_cp = time.time()
                if time.time() - last_git_sync > 1800:
                    git_sync("Log Update (Phonon Running)")
                    last_git_sync = time.time()

                try:
                    if psutil.virtual_memory().percent > MEMORY_LIMIT_PERCENT:
                        print("      ⚠️ RAM NOT-AUS (Python Monitor)!")
                        process.kill()
                        return "OOM"
                except: pass

        except: 
            process.kill()
            return "CRASH"
        
        if process.returncode == -9: return "OOM"

        try:
            with open(output_file, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read(): return "DONE"
        except: pass

        res = analyze_crash_reason(output_file)
        return res if res != "NONE" else "CRASH"

# =============================================================================
# 4. HAUPTPROGRAMM - EINGEBETTET IN DEINEN GLOBALEN TRY-EXCEPT
# =============================================================================
def main():
    # ZUERST in die Datei schreiben, dann den Pull machen
    with open(TXT_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'='*40}\n🚀 NEUSTART SMART-PIPELINE, {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*40}\n")
        f.write("☁️ Führe initialen Git Pull aus...\n")
    
    print("☁️ Führe initialen Git Pull aus...")
    initial_git_pull()
    
    set_logic_app_state("Enabled")
    
    if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
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

        if "SKIPPED" in last_status or "Isolator" in last_status:
            continue

        if "Metall" in last_status and stability in ["STABIL", "INSTABIL"]:
            continue

        crash_type = analyze_crash_reason(scf_out)
        if crash_type == "NON_CONVERGED":
            continue
        
        if not os.path.exists(work_dir): os.makedirs(work_dir)
        msg = f"\n💎 Job, {name}"
        print(msg)
        with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg + "\n")
        
        scf_in = os.path.join(work_dir, "scf.in")
        ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

        if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)

        # Die Phonon-Logik (Phase 2), wenn SCF schon existiert
        if os.path.exists(scf_out) and "JOB DONE" in open(scf_out, errors='ignore').read():
            
            with open(scf_in, 'r') as f: 
                match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", f.read())
                prefix = match.group(1) if match else "calc"
                
            e_fermi = "-"
            with open(scf_out, 'r', errors='ignore') as f:
                match = re.search(r"the Fermi energy is\s+([0-9\.\-]+)\s+ev", f.read())
                if match: e_fermi = float(match.group(1))

            # MANUELLER RESET: Wird NUR ausgelöst, wenn ph.in UND ph.out fehlen.
            if not os.path.exists(ph_in) and not os.path.exists(ph_out):
                msg = "   🧹 Manueller Reset erkannt. Bereinige Phononen-Daten..."
                print(msg)
                with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg + "\n")
                
                shutil.rmtree(os.path.join(work_dir, "tmp", "_ph0"), ignore_errors=True)
                for ext in ["*.dvscf*", "*.a2Fsave*", "*.dyn*", "*.fc", "*.freq", "*.phdos"]:
                    for f in glob.glob(os.path.join(work_dir, "tmp", ext)) + glob.glob(os.path.join(work_dir, ext)):
                        try: os.remove(f)
                        except: pass
                
                with open(ph_in, "w") as f:
                    f.write(f"Phonons\n&INPUTPH\n tr2_ph=1.0d-14, prefix='{prefix}', outdir='./tmp', fildyn='{name}.dyn', ldisp=.true., nq1=2, nq2=2, nq3=2 /\n")

            if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                ph_attempts = 0
                
                while ph_attempts < MAX_RETRIES_LEVEL:
                    ph_attempts += 1
                    ph_res = run_monitored_ph(ph_in, ph_out, work_dir, int(DEFAULT_CORES))
                    
                    if ph_res == "DONE": break
                    
                    print_error_tail(ph_out, 50)
                    
                    if ph_res in ["XML_ERROR", "ELPH_CORRUPT"]:
                        msg_err = f"      ⚠️ Korrupte Datenbank erkannt ({ph_res})!"
                        print(msg_err)
                        with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg_err + "\n")
                        
                        if ph_attempts <= 2 and restore_rolling_checkpoint(work_dir, prefix):
                            msg_cp = "      🔄 Checkpoint geladen. Nächster Versuch..."
                            print(msg_cp)
                            with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg_cp + "\n")
                        else:
                            # HIER IST DEINE REGEL: Nicht mehr den _ph0 Ordner löschen!
                            msg_abort = "      💥 Checkpoints nutzlos. ABBRUCH! Bitte ph.in UND ph.out manuell löschen für Reset."
                            print(msg_abort)
                            with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg_abort + "\n")
                            break # Bricht die Schleife ab, geht zum nächsten System
                        continue

                    msg_retry = f"      🧨 ERROR Phonon-Crash ({ph_res}) bei Job {name}. Retry {ph_attempts}/{MAX_RETRIES_LEVEL}"
                    print(msg_retry)
                    with open(TXT_LOG_FILE, "a") as f_log: f_log.write(msg_retry + "\n")

            if not os.path.exists(ph_out) or "JOB DONE" not in open(ph_out, errors='ignore').read():
                update_csv(name, "SKIPPED (Phonon Crash)") 
                continue

            q2r_in, q2r_out = os.path.join(work_dir, "q2r.in"), os.path.join(work_dir, "q2r.out")
            matdyn_in, matdyn_out = os.path.join(work_dir, "matdyn.in"), os.path.join(work_dir, "matdyn.out")

            if not (os.path.exists(q2r_out) and "JOB DONE" in open(q2r_out, errors='ignore').read()):
                print("   4️⃣  Q2R...")
                with open(q2r_in, "w") as f: f.write(f"&input\n fildyn='{name}.dyn',\n zasr='simple',\n flfrc='{name}.fc',\n la2F=.true.\n/\n")
                with open(q2r_in, "r") as fi, open(q2r_out, "w") as fo:
                    subprocess.run([Q2R_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

            if not (os.path.exists(matdyn_out) and "JOB DONE" in open(matdyn_out, errors='ignore').read()):
                print("   5️⃣  Matdyn...")
                with open(matdyn_in, "w") as f: f.write(f"&input\n asr='simple',\n flfrc='{name}.fc',\n flfrq='{name}.freq',\n fildyn='{name}.dyn',\n dos=.true.,\n elph=.true.,\n fildos='{name}.phdos',\n nk1=10, nk2=10, nk3=10\n/\n")
                with open(matdyn_in, "r") as fi, open(matdyn_out, "w") as fo:
                    subprocess.run([MATDYN_EXE], stdin=fi, stdout=fo, stderr=subprocess.STDOUT, cwd=work_dir)

            lam, wlog, tc = "-", "-", "-"
            if os.path.exists(matdyn_out):
                with open(matdyn_out, 'r', errors='ignore') as f:
                    mc = f.read()
                    if "JOB DONE" in mc:
                        ml, mw = re.search(r"lambda\s*=\s*([0-9\.]+)", mc), re.search(r"omega_log\s*=\s*([0-9\.]+)", mc)
                        if ml and mw:
                            lam, wlog = ml.group(1), mw.group(1)
                            tc_v = berechne_tc(wlog, lam)
                            if tc_v != "-": tc = round(tc_v, 3)

            update_csv(name, "Fertig (Metall)", e_fermi, "-", "JA", stab="STABIL", lam=lam, wlog=wlog, tc=tc)
            git_sync(f"Fertig, {name} (Tc={tc}K)")

    send_notification("🎉 Alle Jobs erledigt.")
    with open(SIGNAL_FILE, "w") as f: f.write(f"Status, Fertig\nTimestamp, {time.ctime()}")
    git_sync("🏁 Finaler Sync vor Shutdown")
    
    print("🛑 Deallokiere VM über Azure CLI...")
    if shutil.which("az"):
        subprocess.run(["az", "vm", "deallocate", "--resource-group", RESOURCE_GROUP, "--name", "Supraleiter-HPC-Knoten"], timeout=60)
    
    if os.name != 'nt': os.system("sudo shutdown -h now")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        full_error = f"\n\n🚨 KRITISCHER ABSTURZ ({datetime.now()})\n{e}\n{traceback.format_exc()}\n"
        with open(TXT_LOG_FILE, "a") as f: f.write(full_error)
        git_sync("🚨 Notfall Sync nach Skript-Absturz")
        send_notification(f"🚨 KRITISCHER FEHLER, {e} -> Shutdown.")
        
        if shutil.which("az"):
            subprocess.run(["az", "vm", "deallocate", "--resource-group", RESOURCE_GROUP, "--name", "Supraleiter-HPC-Knoten"], timeout=60)
        if os.name != 'nt': os.system("sudo shutdown -h now")
        sys.exit()