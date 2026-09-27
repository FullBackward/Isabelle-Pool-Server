""" Demo REPL for Isabelle Gym """

import sys
import os
from pathlib import Path

def main():
    script_dir = Path(__file__).parent.absolute()
    repo_root = script_dir.parent
    
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(script_dir))
    
    src_dir = script_dir / 'src'
    if src_dir.exists():
        sys.path.insert(0, str(src_dir))
        src_python = src_dir / 'python'
        if src_python.exists():
            sys.path.insert(0, str(src_python))
    
    os.chdir(script_dir)
    
    try:
        print("starting Isabelle Python REPL frontend...")
        print(f"working directory: {os.getcwd()}")
        print(f"Python path: {sys.path[:3]}")
        print()
        
        from src.python.isabelle_repl import IsabelleRepl
        
        repl = IsabelleRepl()
        repl.start_interaction_loop()
        
    except ImportError as e:
        print(f"import error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nshutting down...")
    except Exception as e:
        print(f"failed to start: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 