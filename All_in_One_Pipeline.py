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
# ==========================================
TELEGRAM_TOKEN = "DEIN_BOT_TOKEN"  # Hier Token einfügen
TELEGRAM_CHAT_ID = "DEINE_CHAT_ID" # Hier Chat-ID einfügen

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(WORK_DIR, "Results")
LOG_FILE = os.path.join(WORK_DIR, "pipeline_error.log")
CSV_FILE = os.path.join(WORK_DIR, "Final_Electronic_Check.csv")
TMP_DIR = os.path.join(WORK_DIR, "Global_Tmp")

def send_notification(message):
    """Sendet eine Nachricht an dein Handy via Telegram."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🛡️ Supraleiter-Cloud: {message}"}
        requests.post(url, data=payload, timeout=10)
    except:
        print("⚠️ Telegram Nachricht konnte nicht gesendet werden.")

def git_sync(message):
    """Synchronisiert den Fortschritt mit GitHub."""
    try:
        subprocess.run(["git", "add", "."], cwd=WORK_DIR)
        subprocess.run(["git", "commit", "-m", message], cwd=WORK_DIR)
        subprocess.run(["git", "push"], cwd=WORK_DIR)
    except Exception as e:
        print(f"⚠️ Git-Push fehlgeschlagen: {e}")

def emergency_shutdown(error_msg):
    """Loggt den Fehler, informiert dich und schaltet den Server aus."""
    full_error = f"{error_msg}\n{traceback.format_exc()}"
    with open(LOG_FILE, "w") as f:
        f.write(full_error)
    
    print(f"🚨 KRITISCHER FEHLER: {error_msg}")
    send_notification(f"STOPP: {error_msg}. Server wird heruntergefahren.")
    git_sync(f"🚨 Fehler-Log: {error_msg}")
    
    # Der ultimative Kostenstopp
    os.system("sudo shutdown -h now")
    sys.exit()

# =============================================================================
# 2. PHYSIK-MODULE MIT ERROR-HANDLING
# ==========================================

def run_qe_step(name, command, cwd, log_path):
    """Führt einen Rechenschritt aus und prüft auf Erfolg."""
    try:
        with open(log_path, "w") as f:
            process = subprocess.Popen(command, shell=True, cwd=cwd, stdout=f, stderr=f)
            process.wait()
        
        # Prüfung: War die Rechnung erfolgreich?
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                content = f.read()
                if "JOB DONE" in content:
                    return True
        return False
    except Exception as e:
        print(f"Fehler in {name}: {e}")
        return False

# =============================================================================
# 3. HAUPT-PIPELINE
# ==========================================

def main():
    try:
        if not os.path.exists(CSV_FILE):
            emergency_shutdown("Final_Electronic_Check.csv nicht gefunden!")

        df = pd.read_csv(CSV_FILE)
        candidates = df[df['Status'].str.contains("⚡", na=False)]
        
        send_notification(f"Start der Pipeline für {len(candidates)} Kandidaten.")

        for _, row in candidates.iterrows():
            candidate = row['Name']
            ph_work_dir = os.path.join(WORK_DIR, f"PHONON_{candidate}")
            os.makedirs(os.path.join(ph_work_dir, "tmp"), exist_ok=True)
            
            # --- 1. SCF ---
            print(f"🚀 Rechne SCF für {candidate}...")
            # (Hier käme der Code zur scf.in Erstellung hin...)
            
            scf_success = run_qe_step("SCF", "pw.x < scf.in", ph_work_dir, "scf.out")
            if not scf_success:
                send_notification(f"⚠️ SCF fehlgeschlagen für {candidate}. Überspringe.")
                continue

            # --- 2. Phononen ---
            print(f"🚀 Rechne Phononen für {candidate}...")
            ph_success = run_qe_step("PH", "ph.x < ph.in", ph_work_dir, "ph.out")
            
            if ph_success:
                # Cleanup nach getaner Arbeit
                shutil.rmtree(os.path.join(ph_work_dir, "tmp"), ignore_errors=True)
                git_sync(f"✅ Fertig: {candidate}")
                send_notification(f"✅ {candidate} erfolgreich abgeschlossen.")
            else:
                send_notification(f"❌ Phononen-Fehler bei {candidate}.")

        # Alles fertig
        send_notification("🎉 Alle Kandidaten berechnet. Fahre System herunter.")
        os.system("sudo shutdown -h now")

    except Exception as e:
        emergency_shutdown(f"Unbekannter Fehler in Main: {str(e)}")

if __name__ == "__main__":
    main()