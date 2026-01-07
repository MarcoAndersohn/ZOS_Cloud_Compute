import os
import shutil
import subprocess
import time
import glob
import re
import sys
import traceback
import requests
import pandas as pd
from ase.io import read

# =============================================================================
# 1. KONFIGURATION (TELEGRAM & PFADE)
# =============================================================================
TELEGRAM_TOKEN = "8589716957:AAHAAU26UrnwOWgL4OytPpmj0dSPnyWNwu0"
TELEGRAM_CHAT_ID = "711461437"

# Automatische Pfadfindung (Cloud-kompatibel)
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
RESULTS_DIR = os.path.join(WORK_DIR, "Results")
LOG_FILE = os.path.join(WORK_DIR, "pipeline_error.log")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

# Engine Suche
def find_qe_exec(tool_names):
    search_paths = [
        r"C:\Users\Acer\Desktop\Quantum_Espresso",
        "/usr/bin", "/usr/local/bin"
    ]
    for path in search_paths:
        for name in tool_names:
            full_path = os.path.join(path, name)
            if os.path.exists(full_path): return full_path
    return None

PW_EXE = find_qe_exec(["pw.exe", "pw.x"])
PH_EXE = find_qe_exec(["ph.exe", "ph.x"])

if not PW_EXE or not PH_EXE:
    print("❌ FEHLER: Programme nicht gefunden!")
    sys.exit()

# =============================================================================
# 2. TELEGRAM & NOTFALL-FUNKTIONEN
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ Supraleiter-Cloud: {message}"}
        requests.post(url, data=payload, timeout=10)
    except:
        pass

def git_sync(message):
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "push"], cwd=WORK_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        pass

def emergency_shutdown(error_msg):
    full_error = f"{error_msg}\n{traceback.format_exc()}"
    with open(LOG_FILE, "w") as f: f.write(full_error)
    print(f"🚨 KRITISCHER FEHLER: {error_msg}")
    send_notification(f"STOPP: {error_msg}. Server wird heruntergefahren.")
    git_sync(f"🚨 Fehler-Log: {error_msg}")
    if os.name != 'nt': os.system("sudo shutdown -h now") 
    sys.exit()

# =============================================================================
# 3. INTELLIGENTE STEUERUNG
# =============================================================================
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
        print(f"    🔧 Optimierung: Iteration {iteration_count} -> Senke Beta auf {target_beta}")
        if beta_match:
            content = content.replace(beta_match.group(0), f"mixing_beta = {target_beta}")
        else:
            if "&ELECTRONS" in content:
                content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_beta = {target_beta},")
        
        if "mixing_ndim" not in content and "&ELECTRONS" in content:
            content = content.replace("&ELECTRONS", f"&ELECTRONS\n mixing_ndim = 12,")
        
        with open(input_file, 'w') as f: f.write(content)
        return True
    return False

def get_last_iteration(output_file):
    if not os.path.exists(output_file): return 0
    try:
        file_size = os.path.getsize(output_file)
        read_size = min(5000, file_size)
        with open(output_file, 'rb') as f:
            if file_size > 5000: f.seek(-5000, 2)
            chunk = f.read().decode('utf-8', errors='ignore')
        matches = re.findall(r"iteration #\s*(\d+)", chunk)
        if matches: return int(matches[-1])
    except: pass
    return 0

def run_monitored_scf(input_file, output_file, cwd):
    was_optimized = False
    while True:
        with open(input_file, 'r') as f: content = f.read()
        restart_mode = 'restart' if (os.path.exists(output_file) and not was_optimized) else 'from_scratch'
        
        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{restart_mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{restart_mode}',")
            
        temp_input = input_file + ".run"
        with open(temp_input, 'w') as f: f.write(content)

        f_in = open(temp_input, 'r')
        f_out = open(output_file, 'a')
        
        print(f"    ▶️  Engine läuft... (Modus: {restart_mode})")
        process = subprocess.Popen([PW_EXE], stdin=f_in, stdout=f_out, cwd=cwd)
        
        killed_for_optimization = False
        was_optimized = False

        try:
            while process.poll() is None:
                time.sleep(5)
                current_iter = get_last_iteration(output_file)
                if update_input_params(input_file, current_iter):
                    print(f"    🛑 Stoppe für Update (Iter: {current_iter})...")
                    process.terminate()
                    try: process.wait(timeout=10)
                    except: process.kill()
                    killed_for_optimization = True
                    was_optimized = True
                    break
        except KeyboardInterrupt:
            process.kill(); return False
        
        f_in.close(); f_out.close()
        if killed_for_optimization:
            time.sleep(2)
            continue 
            
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return True
        return False

# =============================================================================
# 4. HAUPT-PIPELINE
# =============================================================================
def main():
    try:
        if not os.path.exists(CSV_FILE):
            emergency_shutdown("Final_Electronic_Check.csv fehlt!")

        df = pd.read_csv(CSV_FILE)
        candidates = df[df['Status'].str.contains("⚡", na=False)]
        
        send_notification(f"Start Pipeline: {len(candidates)} Kandidaten.")

        for _, row in candidates.iterrows():
            candidate = row['Name']
            ph_work_dir = os.path.join(WORK_DIR, f"PHONON_{candidate}")
            tmp_dir = os.path.join(ph_work_dir, "tmp")
            if not os.path.exists(ph_work_dir): os.makedirs(ph_work_dir)
            if not os.path.exists(tmp_dir): os.makedirs(tmp_dir)

            scf_in = os.path.join(ph_work_dir, "scf.in")
            scf_out = os.path.join(ph_work_dir, "scf.out")
            ph_in = os.path.join(ph_work_dir, "ph.in")
            ph_out = os.path.join(ph_work_dir, "ph.out")

            print(f"\n🎵 Kandidat: {candidate}")

            scf_done = False
            if os.path.exists(scf_out):
                with open(scf_out, 'r') as f: 
                    if "JOB DONE" in f.read(): scf_done = True
            
            if not scf_done:
                source_out = os.path.join(RESULTS_DIR, f"{candidate}.out")
                if not os.path.exists(source_out):
                    print(f"⚠️ Quelldatei fehlt: {source_out}")
                    continue
                    
                atoms = read(source_out, index=-1)
                elements = sorted(list(set(atoms.get_chemical_symbols())))
                pseudo_path = os.path.join(os.path.dirname(PW_EXE), "pseudo").replace("\\", "/") + "/"
                
                # VORBEREITUNG DER LISTEN (Fix für SyntaxError)
                species_str = "".join([f" {el} 1.0 {el}.UPF\n" for el in elements])
                positions_str = "".join([f" {a.symbol} {a.position[0]:.5f} {a.position[1]:.5f} {a.position[2]:.5f}\n" for a in atoms])
                cell_str = "".join([f" {r[0]:.5f} {r[1]:.5f} {r[2]:.5f}\n" for r in atoms.get_cell()])

                scf_content = f"""&CONTROL
 calculation='scf', prefix='{candidate}', outdir='./tmp/', pseudo_dir='{pseudo_path}'
/
&SYSTEM
 ibrav=0, nat={len(atoms)}, ntyp={len(elements)}, ecutwfc=60, ecutrho=480,
 occupations='smearing', smearing='methfessel-paxton', degauss=0.01
/
&ELECTRONS
 conv_thr=1.0d-12, mixing_beta=0.7
/
ATOMIC_SPECIES
{species_str}
ATOMIC_POSITIONS (angstrom)
{positions_str}
CELL_PARAMETERS (angstrom)
{cell_str}
K_POINTS automatic
 3 3 3 0 0 0
"""
                with open(scf_in, "w") as f: f.write(scf_content)
                print(f"    1️⃣  Starte smarte SCF...")
                if not run_monitored_scf(scf_in, scf_out, ph_work_dir):
                    send_notification(f"⚠️ SCF Crash bei {candidate}. Überspringe.")
                    continue
            else:
                print("    ℹ️  SCF bereits fertig.")

            ph_done = False
            if os.path.exists(ph_out):
                with open(ph_out, 'r') as f:
                    if "JOB DONE" in f.read(): ph_done = True
            
            if not ph_done:
                print(f"    2️⃣  Starte Phononen...")
                recover = ".true." if (os.path.exists(ph_out) and os.path.getsize(ph_out) > 500) else ".false."
                ph_content = f"""Phonons
&INPUTPH
  tr2_ph    = 1.0d-12,
  prefix    = '{candidate}',
  outdir    = './tmp/',
  fildyn    = '{candidate}.dyn',
  trans     = .true., epsil = .false., reduce_io = .true.,
  recover   = {recover}
/
0.0 0.0 0.0
"""
                with open(ph_in, "w") as f: f.write(ph_content)
                with open(ph_out, "a") as f:
                    subprocess.run(f'"{PH_EXE}" < ph.in', shell=True, stdout=f, stderr=f, cwd=ph_work_dir)
                
                with open(ph_out, 'r') as f:
                    if "JOB DONE" in f.read(): ph_done = True

            if ph_done:
                print(f"    ✅ {candidate} komplett fertig. Räume auf...")
                shutil.rmtree(tmp_dir, ignore_errors=True) 
                if os.path.exists(scf_in + ".run"): os.remove(scf_in + ".run")
                git_sync(f"Fertig: {candidate}")
                send_notification(f"✅ {candidate} erfolgreich.")
            else:
                print(f"    ⚠️ Phononen nicht konvergiert für {candidate}.")

        send_notification("🎉 Pipeline beendet. Shutdown.")
        if os.name != 'nt': os.system("sudo shutdown -h now")

    except Exception as e:
        emergency_shutdown(f"Main Error: {e}")

if __name__ == "__main__":
    main()