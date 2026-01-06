import os
import shutil
import subprocess
import time
import glob
import re
import sys
import pandas as pd
from ase.io import read

# =============================================================================
# 1. KONFIGURATION & UMGEBUNG (PORTABEL FÜR CLOUD)
# =============================================================================
# Motivation: Wir nutzen relative Pfade, damit das Projekt auf deinem Laptop 
# und dem Azure-Supercomputer ohne manuelle Änderung läuft.
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(WORK_DIR, "Inputs")
RESULTS_DIR = os.path.join(WORK_DIR, "Results")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")

# Engine Suche (Suche in Standardpfaden)
def find_qe_exec(tool_names):
    # Auf Azure liegt QE oft in /usr/bin/, lokal in deinem Desktop-Ordner
    search_paths = [
        r"C:\Users\Acer\Desktop\Quantum_Espresso",
        "/usr/bin",
        "/usr/local/bin"
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
# 2. HAUPTPROGRAMM (PIPELINE)
# =============================================================================
try:
    df = pd.read_csv(CSV_FILE)
    candidates = df[df['Status'].str.contains("⚡", na=False)]

    if len(candidates) == 0:
        print("❌ Keine Kandidaten (⚡) in CSV gefunden.")
        sys.exit()

    for index, row in candidates.iterrows():
        candidate = row['Name']
        # Motivation: Jeder Kandidat bekommt einen eigenen Ordner für Phononen,
        # um die Wellenfunktionen und dyn-Files sauber zu trennen.
        ph_work_dir = os.path.join(WORK_DIR, f"PHONON_{candidate}")
        if not os.path.exists(ph_work_dir): os.makedirs(ph_work_dir)
        
        scf_out_path = os.path.join(ph_work_dir, "scf.out")
        ph_out_path = os.path.join(ph_work_dir, "ph.out")
        tmp_dir = os.path.join(ph_work_dir, "tmp")
        
        print(f"\n🎵 Bearbeite Kandidat: {candidate}")

        # --- 1. SCF CHECK & RUN ---
        run_scf = True
        if os.path.exists(scf_out_path):
            with open(scf_out_path, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read():
                    print("   ℹ️  SCF bereits vorhanden. Überspringe...")
                    run_scf = False

        if run_scf:
            source_out = os.path.join(RESULTS_DIR, f"{candidate}.out")
            atoms = read(source_out, index=-1)
            elements = sorted(list(set(atoms.get_chemical_symbols())))
            pseudo_path = os.path.join(os.path.dirname(PW_EXE), "pseudo").replace("\\", "/") + "/"
            
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
{"".join([f" {el} 1.0 {el}.UPF\n" for el in elements])}
ATOMIC_POSITIONS (angstrom)
{"".join([f" {a.symbol} {a.position[0]:.5f} {a.position[1]:.5f} {a.position[2]:.5f}\n" for a in atoms])}
CELL_PARAMETERS (angstrom)
{"".join([f" {r[0]:.5f} {r[1]:.5f} {r[2]:.5f}\n" for r in atoms.get_cell()])}
K_POINTS automatic
 3 3 3 0 0 0
"""
            with open(os.path.join(ph_work_dir, "scf.in"), "w") as f: f.write(scf_content)
            print("   1️⃣  Starte SCF...")
            subprocess.run(f'"{PW_EXE}" < scf.in > scf.out', shell=True, cwd=ph_work_dir)

        # --- 2. PHONONEN CHECK & RUN ---
        already_done = False
        if os.path.exists(ph_out_path):
            with open(ph_out_path, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read():
                    print(f"   ✅ {candidate}: Phononen bereits fertig!")
                    already_done = True
        
        if not already_done:
            # Motivation: Wir nutzen 'recover', falls der Supercomputer uns 
            # während der Rechnung rauswirft.
            recover_val = ".true." if os.path.exists(ph_out_path) and os.path.getsize(ph_out_path) > 500 else ".false."
            
            ph_content = f"""Phonons
&INPUTPH
  tr2_ph    = 1.0d-12,
  prefix    = '{candidate}',
  outdir    = './tmp/',
  fildyn    = '{candidate}.dyn',
  trans     = .true., epsil = .false., reduce_io = .true.,
  recover   = {recover_val}
/
0.0 0.0 0.0
"""
            with open(os.path.join(ph_work_dir, "ph.in"), "w") as f: f.write(ph_content)
            print(f"   2️⃣  Phononen-Lauf (Recover: {recover_val})...")
            subprocess.run(f'"{PH_EXE}" < ph.in > ph.out', shell=True, cwd=ph_work_dir)

        # --- 3. CLEANUP (NUR BEI ERFOLG) ---
        # Motivation: Wellenfunktionen verbrauchen Gigabytes. Nach getaner Arbeit 
        # löschen wir den tmp-Ordner, um Speicher für Azure zu sparen.
        if os.path.exists(ph_out_path):
            with open(ph_out_path, 'r', errors='ignore') as f:
                if "JOB DONE" in f.read():
                    if os.path.exists(tmp_dir):
                        print(f"   🧹 Cleanup für {candidate}...")
                        shutil.rmtree(tmp_dir)

except Exception as e:
    print(f"\n❌ FEHLER im Hauptprogramm: {e}")

print("\n=== PIPELINE BEENDET ===")