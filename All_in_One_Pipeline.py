import os
import shutil
import subprocess
import time
import glob
import re
import sys
import traceback
import requests

# =============================================================================
# 1. KONFIGURATION
# =============================================================================
TELEGRAM_TOKEN = "8589716957:AAHAAU26UrnwOWgL4OytPpmj0dSPnyWNwu0"
TELEGRAM_CHAT_ID = "711461437"

# WICHTIG: Hier den exakten Namen deiner Logic App eintragen!
LOGIC_APP_NAME = "DEIN_LOGIC_APP_NAME_HIER_EINTRAGEN" 
RESOURCE_GROUP = "Supraleiter-HPC-Knoten_group"

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")
LOG_FILE = os.path.join(WORK_DIR, "pipeline_error.log")
SIGNAL_FILE = os.path.join(WORK_DIR, "rechnung_fertig.txt") 

# Engine Suche
def find_qe_exec(tool_names):
    search_paths = ["/usr/bin", "/usr/local/bin", r"C:\Quantum_Espresso", os.path.expanduser("~")+"/bin"]
    for path in search_paths:
        for name in tool_names:
            full_path = os.path.join(path, name)
            if os.path.exists(full_path): return full_path
    return None

PW_EXE = find_qe_exec(["pw.x", "pw.exe"])
PH_EXE = find_qe_exec(["ph.x", "ph.exe"])
DOS_EXE = find_qe_exec(["dos.x", "dos.exe"])

if not PW_EXE or not PH_EXE or not DOS_EXE:
    print("❌ FEHLER: Quantum Espresso Programme (pw.x, ph.x oder dos.x) nicht gefunden!")
    sys.exit()

# =============================================================================
# 2. NOTFALL & SHUTDOWN & LOGIC APP DISABLE
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ Supraleiter (HPC): {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def git_sync(message):
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR)
        subprocess.run(["git", "push"], cwd=WORK_DIR)
    except: pass

def disable_logic_app():
    """Deaktiviert die Azure Logic App, um Kosten zu sparen."""
    print(f"🛑 Deaktiviere Logic App: {LOGIC_APP_NAME}...")
    try:
        # Azure CLI Befehl zum Deaktivieren des Workflows
        cmd = [
            "az", "logic", "workflow", "set-state",
            "--resource-group", RESOURCE_GROUP,
            "--name", LOGIC_APP_NAME,
            "--state", "Disabled"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Logic App erfolgreich deaktiviert.")
            send_notification("Logic App wurde in den Schlafmodus versetzt (Disabled).")
        else:
            print(f"⚠️ Fehler beim Deaktivieren der Logic App: {result.stderr}")
            send_notification(f"Warnung: Konnte Logic App nicht deaktivieren! Fehler: {result.stderr}")
    except Exception as e:
        print(f"⚠️ Exception beim Deaktivieren: {e}")

def smart_shutdown(reason="Fertig"):
    print(f"\n🔌 Leite Shutdown ein: {reason}")
    
    # 1. Logic App deaktivieren (Damit sie nicht wieder aufwacht)
    disable_logic_app()

    # 2. Signaldatei erstellen (Als Backup, falls Logic App doch läuft)
    try:
        with open(SIGNAL_FILE, "w") as f:
            f.write(f"Status: {reason}\nTimestamp: {time.ctime()}")
    except: pass

    # 3. Versuche harte Deallokation via Azure CLI
    try:
        print("⏳ Hole Azure Metadaten...")
        meta_url = "http://169.254.169.254/metadata/instance?api-version=2021-02-01"
        headers = {"Metadata": "true"}
        data = requests.get(meta_url, headers=headers, timeout=2).json()
        
        vm_name = data['compute']['name']
        rg_name = data['compute']['resourceGroupName']
        
        print(f"🚀 Deallokiere VM {vm_name}...")
        subprocess.run(f"az vm deallocate --resource-group {rg_name} --name {vm_name}", shell=True)
    except:
        print("⚠️ Azure CLI oder Metadaten fehlgeschlagen. Fallback auf System-Shutdown.")
        if os.name != 'nt': os.system("sudo shutdown -h now")

def emergency_shutdown(error_msg):
    full_error = f"{error_msg}\n{traceback.format_exc()}"
    with open(LOG_FILE, "w") as f: f.write(full_error)
    send_notification(f"🚨 STOPP: {error_msg}")
    git_sync(f"🚨 Fehler: {error_msg}")
    smart_shutdown(reason="Emergency Error")
    sys.exit()

# =============================================================================
# 3. PUPPET MASTER (LOGIK)
# =============================================================================
def get_prefix_from_content(content):
    match = re.search(r"prefix\s*=\s*['\"]([^'\"]+)['\"]", content)
    return match.group(1) if match else "calc"

def update_input_params(input_file, iteration_count):
    target_beta = 0.7
    if iteration_count >= 90: target_beta = 0.15
    elif iteration_count >= 60: target_beta = 0.25
    elif iteration_count >= 30: target_beta = 0.4
    else: return False

    with open(input_file, 'r') as f: content = f.read()
    beta_match = re.search(r"mixing_beta\s*=\s*([0-9\.]+)", content)
    current_beta = float(beta_match.group(1)) if beta_match else 0.7

    if abs(current_beta - target_beta) > 0.01:
        print(f"    🔧 Puppet Master: Senke Beta auf {target_beta} (Iter: {iteration_count})")
        content = re.sub(r"mixing_beta\s*=\s*[0-9\.]+", f"mixing_beta = {target_beta}", content)
        if "mixing_ndim" not in content:
            content = content.replace("&ELECTRONS", "&ELECTRONS\n mixing_ndim = 12,")
        with open(input_file, 'w') as f: f.write(content)
        return True
    return False

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
    with open(input_file, 'r') as f: content = f.read()
    corr_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" not in content:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{corr_path}',")
    with open(input_file, 'w') as f: f.write(content)

    while True:
        with open(input_file, 'r') as f: content = f.read()
        mode = 'restart' if (os.path.exists(output_file)) else 'from_scratch'
        if "restart_mode" not in content:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
        else:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
            
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        file_mode = 'a' if mode == 'restart' else 'w'
        
        with open(run_input, 'r') as f_in, open(output_file, file_mode) as f_out:
            cmd = ["mpirun", "--oversubscribe", "-np", "2", PW_EXE]
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, cwd=cwd)
            
            killed = False
            try:
                while process.poll() is None:
                    time.sleep(10)
                    cur_iter = get_last_iteration(output_file)
                    
                    if update_input_params(input_file, cur_iter):
                        process.terminate()
                        killed = True
                        try:
                            timestamp = int(time.time())
                            shutil.move(output_file, f"{output_file}.bak_{timestamp}")
                            print(f"    🧹 Log rotiert -> {output_file}.bak_{timestamp}")
                        except: pass
                        break
            except: process.kill(); return False
            
        if killed: continue 
        
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return True
        return False

# =============================================================================
# 4. HAUPTPROGRAMM
# =============================================================================
def main():
    try:
        # Alte Signaldatei löschen, falls vorhanden (startet clean)
        if os.path.exists(SIGNAL_FILE): os.remove(SIGNAL_FILE)
        
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        
        input_files = glob.glob(os.path.join(INPUTS_DIR, "*.in"))
        
        if not input_files:
            # Keine Arbeit da -> Logic App deaktivieren und runterfahren
            print("⚠️ Keine Inputs gefunden.")
            smart_shutdown("Leerlauf - Keine Inputs")
            sys.exit()

        send_notification(f"Start: {len(input_files)} Jobs.")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            print(f"\n💎 Job: {name}")
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            if not os.path.exists(work_dir): os.makedirs(work_dir)
            
            scf_in = os.path.join(work_dir, "scf.in")
            scf_out = os.path.join(work_dir, "scf.out")
            dos_in, dos_out = os.path.join(work_dir, "dos.in"), os.path.join(work_dir, f"{name}.dos")
            ph_in, ph_out = os.path.join(work_dir, "ph.in"), os.path.join(work_dir, "ph.out")

            # --- 1. SCF ---
            if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)
            scf_done = False
            if os.path.exists(scf_out) and "JOB DONE" in open(scf_out).read(): scf_done = True
            
            if not scf_done:
                print("    1️⃣  Starte SCF...")
                if not run_monitored_pw(scf_in, scf_out, work_dir):
                    print(f"    ❌ SCF fehlgeschlagen."); continue
            
            with open(scf_in, 'r') as f: scf_content = f.read()
            prefix = get_prefix_from_content(scf_content)

            # --- 2. DOS ---
            if not os.path.exists(dos_out):
                print("    2️⃣  DOS Berechnung...")
                dos_content = f"""&DOS
  prefix='{prefix}',
  outdir='./tmp',
  fildos='{name}.dos',
  Emin=-20.0, Emax=30.0, DeltaE=0.1
/
"""
                with open(dos_in, "w") as f: f.write(dos_content)
                with open(dos_in, "r") as f_in, open(dos_out, "w") as f_out:
                    subprocess.run([DOS_EXE], stdin=f_in, stdout=f_out, cwd=work_dir)

            # --- 3. PHONONEN ---
            if not os.path.exists(ph_out):
                print("    3️⃣  Phononen Berechnung...")
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
                    cmd_ph = ["mpirun", "--oversubscribe", "-np", "2", PH_EXE]
                    subprocess.run(cmd_ph, stdin=f_in, stdout=f_out, cwd=work_dir)

            git_sync(f"Fertig: {name}")

        send_notification("🎉 Alles erledigt.")
        smart_shutdown("Pipeline Success - Alles fertig")

    except Exception as e: emergency_shutdown(f"Error: {e}")

if __name__ == "__main__":
    main()