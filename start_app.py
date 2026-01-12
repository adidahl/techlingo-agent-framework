import subprocess
import time
import signal
import sys
import os
from pathlib import Path

def start_services():
    # Paths
    root_dir = Path(__file__).parent
    web_dir = root_dir / "web"
    
    env = os.environ.copy()
    
    print("🚀 Starting TechLingo Web App...")

    # Start Backend (FastAPI)
    print("Starting Backend (Port 8000)...")
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.main:app", "--reload", "--port", "8000"],
        cwd=str(root_dir),
        env=env
    )
    
    # Start Frontend (Next.js)
    print("Starting Frontend (Port 3000)...")
    frontend = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=str(web_dir),
        env=env
    )

    print("\n✅ Services started!")
    print("Backend: http://localhost:8000")
    print("Frontend: http://localhost:3000/generator")
    print("\nPress Ctrl+C to stop both services.\n")

    try:
        while True:
            time.sleep(1)
            # Check if processes are still alive
            if backend.poll() is not None:
                print("Backend process exited unexpectedly.")
                break
            if frontend.poll() is not None:
                print("Frontend process exited unexpectedly.")
                break
    except KeyboardInterrupt:
        print("\nStopping services...")
    finally:
        # Graceful shutdown
        backend.terminate()
        frontend.terminate()
        
        # Wait for them to exit
        backend.wait()
        frontend.wait()
        print("Services stopped.")

if __name__ == "__main__":
    start_services()
