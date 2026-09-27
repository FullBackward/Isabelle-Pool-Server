import time
from typing import Optional

from .repl_backend_gateway import EnvStateID, ReplBackendGatewayProcess, ReplResult, Outputs
from .thy_init import ThyInit
from .operation import Success, Failure


class IsabelleClient:

    def __init__(
        self, 
        show_states: bool = False,
        enable_cache: bool = False,
        max_cache_size: int = 10,
        shared_cache: bool = False,
        initial_thys: list[str] = None,
        field: str = "HOL"
    ) -> None:
        self.repl_backend_gateway_process = ReplBackendGatewayProcess()
        self.repl_backend_gateway_process_init = True

        self.show_states = show_states
        self.enable_cache = enable_cache
        self.max_cache_size = max_cache_size
        self.shared_cache = shared_cache
        self.thy_init = ThyInit()
        self.main_session_field = field
        if self.thy_init is None:
            raise RuntimeError("Failed to initialize ThyInit: init.thy file not found.")
        
        self.default_session_theories = self._normalize_theories(initial_thys)
        self.initial_thys, self.initial_wrapper_name = self._build_loaded_theories(
            wrapper_name="main",
            dependency_theories=self.default_session_theories,
        )
        
        java_list = self.repl_backend_gateway_process.gateway.jvm.java.util.ArrayList()
        for thy in self.initial_thys:
            java_list.add(thy)
        
        if shared_cache:
            self.repl_backend = self.repl_backend_gateway_process.get_repl_backend_with_shared_cache(
                show_states, enable_cache, max_cache_size, java_list, self.main_session_field
            )

            self.use_multi_session = True
            self.sessions = {}
            self.current_session_id = None
            #self.create_session("default", initial_thys)
        else:

            self.repl_backend = self.repl_backend_gateway_process.get_repl_backend_with_initial_theories(
                show_states, enable_cache, max_cache_size, java_list, self.main_session_field
            )
            self.use_multi_session = False
            self.sessions = None
            self.current_session_id = None
        
        self.repl_backend_init = True

    def _normalize_theories(self, theories: Optional[list[str]]) -> list[str]:
        if theories is None:
            return []

        normalized: list[str] = []
        for theory in theories:
            if theory is None:
                continue
            value = str(theory).strip()
            if value:
                normalized.append(value)

        return normalized

    def _build_loaded_theories(
        self, wrapper_name: str, dependency_theories: list[str]
    ) -> tuple[list[str], Optional[str]]:
        if not dependency_theories:
            return ["$ISABELLE_REPL_HOME/thys/IsabelleREPL"], None

        gen_result = self.thy_init.gen_file(wrapper_name, dependency_theories)
        if gen_result.__class__ != Success or not getattr(gen_result, "data", None):
            raise RuntimeError(
                f"Failed to generate theory wrapper {wrapper_name}: "
                f"{getattr(gen_result, 'err', 'unknown ThyInit error')}"
            )

        wrapper_theory = str(gen_result.data)
        return [f"$ISABELLE_REPL_HOME/thys/{wrapper_theory}"], wrapper_theory

    def _get_active_backend(self):
        if self.use_multi_session:
            return self.get_current_session()
        else:
            return self.repl_backend

    def enter_thy(self, thy_name: str) -> ReplResult:
        return self._get_active_backend().enter_thy(thy_name)

    def isar_snippet(self, isar_string: str) -> ReplResult:
        if(isar_string.strip() == ""):
            return ReplResult(Outputs("", ""), "Empty input")
        return self._get_active_backend().step(isar_string)

    def get_current_thy_name(self) -> str:
        return self._get_active_backend().current_thy_name_string()

    def open_subgoals(self) -> list[str]:
        subgoals = self._get_active_backend().open_subgoals()
        return [subgoal.strip() for subgoal in subgoals]

    def local_facts(self) -> list[str]:
        return list(self._get_active_backend().local_facts())

    def global_facts(self, limit: int) -> list[str]:
        return list(self._get_active_backend().global_facts(limit))

    def get_source(self) -> ReplResult:
        return self._get_active_backend().get_source()

    def rollback(self) -> ReplResult:
        return self._get_active_backend().rollback()

    def save_state(self) -> EnvStateID:
        return self._get_active_backend().save_state()

    def restore_state(self, state_id: EnvStateID) -> bool:
        return self._get_active_backend().restore_state(state_id)

    def reset(self) -> None:
        if self.use_multi_session:
            self._get_active_backend().reset()
        else:
            self.repl_backend.reset()

    def vectorise(self, size: int) -> None:
        return self._get_active_backend().vectorise(size)

    def scalarise(self, index_to_keep: int):
        return self._get_active_backend().scalarise(index_to_keep)

    def vector_step(self, isar_strings: list[str]) -> ReplResult:
        return self._get_active_backend().vector_step(isar_strings)

    def cleanup(self) -> None:
    
        if not (self.repl_backend_gateway_process_init 
                and not self.repl_backend_gateway_process.has_terminated()):
            return
    
        print("[Cleanup] Starting Isabelle cleanup sequence...")
    
        # Step 1: Close all sessions/backends gracefully
        if self.use_multi_session:
            print(f"[Cleanup] Closing {len(self.sessions)} sessions...")
            for session_id in list(self.sessions.keys()):
                try:
                    self.close_session(session_id)
                    print(f"Closed session: {session_id}")
                except Exception as e:
                    print(f"Error closing session {session_id}: {e}")
        else:
            print("[Cleanup] Closing backend...")
            try:
                self.repl_backend.exit()
                print("Backend exited")
            except Exception as e:
                print(f"Error closing backend: {e}")
            if self.initial_wrapper_name is not None:
                result = self.thy_init.cleanup(self.initial_wrapper_name)
                if result.__class__ != Success:
                    print(f"Error cleaning up main theory file: {result.err}")
                else:
                    print("Main theory files cleaned up")

    
        # Step 2: CRITICAL - Give backends time to cleanup
        print("[Cleanup] Waiting for backends to finish cleanup...")
        time.sleep(0.5)  # 500ms grace period
    
        # Step 3: Terminate gateway process
        print("[Cleanup] Terminating gateway process...")
        try:
            self.repl_backend_gateway_process.terminate()
            print("Gateway terminated")
        except Exception as e:
            print(f"Gateway termination warning: {e}")
    
        print("[Cleanup] Cleanup complete")

    def get_cache_stats(self) -> dict:
        if self.use_multi_session:
            return self.get_session_cache_stats()
        else:
            return dict(self.repl_backend.get_cache_stats())

    def get_cache_status(self) -> str:
        if self.use_multi_session:
            return self.get_session_cache_status()
        else:
            return self.repl_backend.get_cache_status()

    # only available in multi-session mode
    def create_session(self, session_id: str, initial_thys: list[str] = None, field: str = "HOL") -> str:
        if not self.use_multi_session:
            raise RuntimeError("create_session is only available in multi-session mode")

        dependency_theories = (
            self.default_session_theories if initial_thys is None else self._normalize_theories(initial_thys)
        )
        loaded_thys, wrapper_name = self._build_loaded_theories(session_id, dependency_theories)

        java_list = self.repl_backend_gateway_process.gateway.jvm.java.util.ArrayList()
        for thy in loaded_thys:
            java_list.add(thy)

        # use shared cache in multi-session mode
        new_backend = self.repl_backend_gateway_process.get_repl_backend_with_shared_cache(
            self.show_states, self.enable_cache, self.max_cache_size, java_list, field
        )

        self.sessions[session_id] = {
            'backend': new_backend,
            'session_theories': dependency_theories,
            'initial_thys': loaded_thys,
            'wrapper_name': wrapper_name,
            'created_at': time.time()
        }

        if self.current_session_id is None:
            self.current_session_id = session_id

        print(f"create Session: {session_id}, theories: {loaded_thys}")
        return session_id

    def switch_session(self, session_id: str) -> bool:
        if not self.use_multi_session:
            raise RuntimeError("switch_session is only available in multi-session mode")
        
        if session_id not in self.sessions:
            print(f"Session {session_id} does not exist")
            return False
        
        self.current_session_id = session_id
        print(f"switch to Session: {session_id}")
        return True
    
    def get_current_session(self):
        if not self.use_multi_session:
            raise RuntimeError("get_current_session is only available in multi-session mode")
        
        if self.current_session_id is None:
            raise RuntimeError("No active Session")
        return self.sessions[self.current_session_id]['backend']
    
    def list_sessions(self) -> dict:
        """list all Sessions"""
        if not self.use_multi_session:
            raise RuntimeError("list_sessions is only available in multi-session mode")
        
        result = {}
        for session_id, session_info in self.sessions.items():
            result[session_id] = {
                'session_theories': session_info['session_theories'],
                'initial_thys': session_info['initial_thys'],
                'created_at': session_info['created_at'],
                'is_current': session_id == self.current_session_id
            }
        return result
    
    def close_session(self, session_id: str) -> bool:
        if not self.use_multi_session:
            raise RuntimeError("close_session is only available in multi-session mode")
        
        if session_id not in self.sessions:
            print(f"Session {session_id} does not exist")
            return False
        
        if session_id == self.current_session_id:
            self.current_session_id = None
        
        session_info = self.sessions.pop(session_id)
        wrapper_name = session_info.get('wrapper_name')
        if wrapper_name is not None:
            result = self.thy_init.cleanup(wrapper_name)
            if not result.__class__ == Success:
                print(f"Warning: Failed to cleanup theory file for session {session_id}: {result.err}")
        #try:
        #    session_info['backend'].exit()
        #except:
        #    pass
        
        print(f"close Session: {session_id}")
        return True
    
    def get_session_cache_stats(self, session_id: str = None) -> dict:
        if not self.use_multi_session:
            raise RuntimeError("get_session_cache_stats is only available in multi-session mode")
        
        if session_id is None:
            session_id = self.current_session_id
        
        if session_id not in self.sessions:
            return {}
        
        backend = self.sessions[session_id]['backend']
        return dict(backend.get_cache_stats())
    
    def get_session_cache_status(self, session_id: str = None) -> str:
        if not self.use_multi_session:
            raise RuntimeError("get_session_cache_status is only available in multi-session mode")
        
        if session_id is None:
            session_id = self.current_session_id
        
        if session_id not in self.sessions:
            return "Session does not exist"
        
        backend = self.sessions[session_id]['backend']
        return backend.get_cache_status()