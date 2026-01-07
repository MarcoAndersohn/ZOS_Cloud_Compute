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

# Pfade relativ zum Skript
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
RESULTS_DIR = os.path.join(WORK_DIR, "Results")
PSEUDO_DIR = os.path.join(WORK_DIR, "pseudo")  # WICHTIG: Hier müssen die .UPF Dateien liegen
LOG_FILE = os.path.join(WORK_DIR, "pipeline_error.log")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

# Engine Suche
def find_qe_exec(tool_names):
    search_paths = ["/usr/bin", "/usr/local/bin", r"C:\Quantum_Espresso"]
    for path in search_paths:
        for name in tool_names:
            full_path = os.path.join(path, name)
            if os.path.exists(full_path): return full_path
    return None

PW_EXE = find_qe_exec(["pw.x", "pw.exe"])
PH_EXE = find_qe_exec(["ph.x", "ph.exe"])
DOS_EXE = find_qe_exec(["dos.x", "dos.exe"])

if not PW_EXE or not PH_EXE or not DOS_EXE:
    print("❌ FEHLER: Quantum Espresso Programme (pw, ph, dos) nicht gefunden!")
    sys.exit()

# =============================================================================
# 2. HILFSFUNKTIONEN (NOTFALL & SYNC)
# =============================================================================
def send_notification(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ Supraleiter-Fabrik: {message}"}
        requests.post(url, data=payload, timeout=10)
    except: pass

def git_sync(message):
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR)
        subprocess.run(["git", "push"], cwd=WORK_DIR)
    except: pass

def emergency_shutdown(error_msg):
    full_error = f"{error_msg}\n{traceback.format_exc()}"
    with open(LOG_FILE, "w") as f: f.write(full_error)
    send_notification(f"🚨 STOPP: {error_msg}. Server-Shutdown.")
    git_sync(f"🚨 Fehler-Log: {error_msg}")
    if os.name != 'nt': os.system("sudo shutdown -h now")
    sys.exit()

# =============================================================================
# 3. PUPPET MASTER (KONVERGENZ-HELFER)
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
        with open(output_file, 'rb') as f:
            f.seek(max(0, os.path.getsize(output_file) - 5000), 0)
            chunk = f.read().decode('utf-8', errors='ignore')
        matches = re.findall(r"iteration #\s*(\d+)", chunk)
        return int(matches[-1]) if matches else 0
    except: return 0

def run_monitored_pw(input_file, output_file, cwd):
    """Führt pw.x mit 8 Kernen aus und greift ein, wenn es nicht konvergiert."""
    was_optimized = False
    
    # --- AUTO-FIX: PFADE REPARIEREN ---
    with open(input_file, 'r') as f: content = f.read()
    correct_pseudo_path = PSEUDO_DIR.replace("\\", "/") + "/"
    if "pseudo_dir" in content:
        content = re.sub(r"pseudo_dir\s*=\s*['\"].*?['\"]", f"pseudo_dir='{correct_pseudo_path}'", content)
    else:
        content = content.replace("&CONTROL", f"&CONTROL\n pseudo_dir='{correct_pseudo_path}',")
    with open(input_file, 'w') as f: f.write(content)
    # ----------------------------------

    while True:
        with open(input_file, 'r') as f: content = f.read()
        mode = 'restart' if (os.path.exists(output_file) and not was_optimized) else 'from_scratch'
        
        if "restart_mode" in content:
            content = re.sub(r"restart_mode\s*=\s*['\"].*['\"]", f"restart_mode='{mode}'", content)
        else:
            content = content.replace("&CONTROL", f"&CONTROL\n restart_mode='{mode}',")
            
        run_input = input_file + ".run"
        with open(run_input, 'w') as f: f.write(content)

        with open(run_input, 'r') as f_in, open(output_file, 'a') as f_out:
            # HIER IST DER TURBO: mpirun -np 8
            # Wir nutzen eine Liste für Popen, damit Argumente sauber getrennt sind
            cmd = ["mpirun", "-np", "8", PW_EXE]
            process = subprocess.Popen(cmd, stdin=f_in, stdout=f_out, cwd=cwd)
            
            killed = False
            try:
                while process.poll() is None:
                    time.sleep(10)
                    cur_iter = get_last_iteration(output_file)
                    if update_input_params(input_file, cur_iter):
                        process.terminate()
                        killed = True; was_optimized = True
                        break
            except: process.kill(); return False
            
        if killed: continue
        with open(output_file, 'r') as f:
            if "JOB DONE" in f.read(): return True
        return False

# =============================================================================
# 4. DOS ANALYSE FUNKTION
# =============================================================================
def check_is_metal(dos_file):
    try:
        with open(dos_file, 'r') as f: lines = f.readlines()
        e_fermi = None
        for line in lines[:30]: 
            if "EFermi" in line or "Fermi" in line:
                parts = line.split("EFermi =")
                if len(parts) > 1:
                    e_fermi = float(parts[1].split("eV")[0])
                    break
        
        if e_fermi is None: return False, 0.0

        dos_at_fermi = 0.0
        closest_diff = 999.9
        
        for line in lines:
            if line.strip().startswith("#"): continue
            parts = line.split()
            if len(parts) < 2: continue
            try:
                energy = float(parts[0])
                dos_val = float(parts[1])
                diff = abs(energy - e_fermi)
                if diff < closest_diff:
                    closest_diff = diff
                    dos_at_fermi = dos_val
            except: continue
            
        return (dos_at_fermi > 0.05), dos_at_fermi
    except:
        return False, 0.0

# =============================================================================
# 5. MAIN PIPELINE
# =============================================================================
def main():
    try:
        if not os.path.exists(INPUTS_DIR): os.makedirs(INPUTS_DIR)
        input_files = glob.glob(os.path.join(INPUTS_DIR, "*.in"))
        
        if not input_files:
            print("⚠️ Keine .in Dateien im Inputs-Ordner gefunden!")
            sys.exit()

        send_notification(f"Start: {len(input_files)} Kandidaten auf 8 Kernen.")

        for input_file in input_files:
            name = os.path.basename(input_file).replace(".in", "")
            print(f"\n💎 Kandidat: {name}")
            
            work_dir = os.path.join(WORK_DIR, f"RUN_{name}")
            if not os.path.exists(work_dir): os.makedirs(work_dir)
            
            scf_in = os.path.join(work_dir, "scf.in")
            scf_out = os.path.join(work_dir, "scf.out")
            nscf_in = os.path.join(work_dir, "nscf.in")
            nscf_out = os.path.join(work_dir, "nscf.out")
            dos_in = os.path.join(work_dir, "dos.in")
            dos_out = os.path.join(work_dir, f"{name}.dos")
            ph_in = os.path.join(work_dir, "ph.in")
            ph_out = os.path.join(work_dir, "ph.out")

            # --- SCHRITT 1: RELAXATION (SCF) ---
            if not os.path.exists(scf_in): shutil.copy(input_file, scf_in)
            
            scf_done = False
            if os.path.exists(scf_out):
                with open(scf_out, 'r') as f: 
                    if "JOB DONE" in f.read(): scf_done = True
            
            if not scf_done:
                print("    1️⃣  Starte Relaxation (SCF) [8 Kerne]...")
                if not run_monitored_pw(scf_in, scf_out, work_dir):
                    print(f"    ❌ SCF fehlgeschlagen für {name}. Überspringe."); continue
            else:
                print("    ℹ️  Relaxation bereits fertig.")

            # --- SCHRITT 2: DOS & METALL CHECK ---
            if not os.path.exists(dos_out):
                print("    2️⃣  Prüfe auf Metall (NSCF + DOS)...")
                try:
                    atoms = read(scf_out, index=-1)
                    elements = sorted(list(set(atoms.get_chemical_symbols())))
                    pseudo_path = PSEUDO_DIR.replace("\\", "/") + "/"
                    
                    spec_str = "".join([f" {el} 1.0 {el}.UPF\n" for el in elements])
                    pos_str = "".join([f" {a.symbol} {a.position[0]:.5f} {a.position[1]:.5f} {a.position[2]:.5f}\n" for a in atoms])
                    cell_str = "".join([f" {r[0]:.5f} {r[1]:.5f} {r[2]:.5f}\n" for r in atoms.get_cell()])

                    nscf_content = f"""&CONTROL
 calculation='nscf', prefix='{name}', outdir='./tmp/', pseudo_dir='{pseudo_path}'
/
&SYSTEM
 ibrav=0, nat={len(atoms)}, ntyp={len(elements)}, ecutwfc=60, ecutrho=480,
 occupations='tetrahedra'
/
&ELECTRONS
 conv_thr=1.0d-8, mixing_beta=0.7
/
ATOMIC_SPECIES
{spec_str}
ATOMIC_POSITIONS (angstrom)
{pos_str}
CELL_PARAMETERS (angstrom)
{cell_str}
K_POINTS automatic
 6 6 6 0 0 0
"""
                    with open(nscf_in, "w") as f: f.write(nscf_content)
                    
                    with open(nscf_out, "w") as f_log:
                        # HIER IST DER TURBO: mpirun -np 8
                        subprocess.run(f'mpirun -np 8 "{PW_EXE}" < nscf.in', shell=True, stdout=f_log, stderr=f_log, cwd=work_dir)
                    
                    dos_content = f"&DOS\n prefix='{name}', outdir='./tmp/', fildos='{name}.dos', Emin=-20.0, Emax=20.0, DeltaE=0.05\n/\n"
                    with open(dos_in, "w") as f: f.write(dos_content)
                    
                    with open(os.path.join(work_dir, "dos.log"), "w") as f_log:
                        # DOS braucht meist kein MPI, aber schadet nicht wenn supported
                        subprocess.run(f'"{DOS_EXE}" < dos.in > {name}.dos', shell=True, cwd=work_dir)

                except Exception as e:
                    print(f"    ❌ Fehler bei DOS-Vorbereitung: {e}"); continue
            
            # --- SCHRITT 3: ENTSCHEIDUNG ---
            is_metal, dos_val = check_is_metal(dos_out)
            status_icon = "⚡" if is_metal else "🧱"
            print(f"    📊 DOS @ Fermi: {dos_val:.3f} -> {status_icon}")

            if not is_metal:
                print(f"    🛑 Kein Metall. Stoppe hier für {name}.")
                git_sync(f"Isolator: {name}")
                continue 

            # --- SCHRITT 4: PHONONEN (NUR WENN METALL) ---
            print("    3️⃣  Starte Phononen (da Metall) [8 Kerne]...")
            
            ph_done = False
            if os.path.exists(ph_out):
                with open(ph_out, 'r') as f:
                    if "JOB DONE" in f.read(): ph_done = True
            
            if not ph_done:
                recover = ".true." if (os.path.exists(ph_out) and os.path.getsize(ph_out) > 500) else ".false."
                ph_content = f"""Phonons
&INPUTPH
  tr2_ph    = 1.0d-12,
  prefix    = '{name}',
  outdir    = './tmp/',
  fildyn    = '{name}.dyn',
  trans     = .true., epsil = .false., reduce_io = .true.,
  recover   = {recover}
/
0.0 0.0 0.0
"""
                with open(ph_in, "w") as f: f.write(ph_content)
                
                with open(ph_out, "a") as f:
                    # HIER IST DER TURBO: mpirun -np 8
                    subprocess.run(f'mpirun -np 8 "{PH_EXE}" < ph.in', shell=True, stdout=f, stderr=f, cwd=work_dir)

            final_success = False
            if os.path.exists(ph_out):
                with open(ph_out, 'r') as f:
                    if "JOB DONE" in f.read(): final_success = True
            
            if final_success:
                print(f"    ✅ {name} komplett fertig!")
                send_notification(f"✅ {name} (Metall) fertig berechnet.")
                shutil.rmtree(os.path.join(work_dir, "tmp"), ignore_errors=True)
                git_sync(f"Fertig: {name}")
            else:
                print("    ⚠️ Phononen noch nicht konvergiert.")

        send_notification("🎉 Pipeline beendet. Shutdown.")
        if os.name != 'nt': os.system("sudo shutdown -h now")

    except Exception as e: emergency_shutdown(f"Main Error: {e}")

if __name__ == "__main__":
    main()