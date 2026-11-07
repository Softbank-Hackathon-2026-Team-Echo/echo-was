import os
import subprocess
import sys


def test_policy_digest_survives_api_worker_process_hash_seed_changes() -> None:
    values = []
    for seed in ["1", "2"]:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.services.auto_dr_policy import security_policy_digest; "
                "print(security_policy_digest())",
            ],
            # 다른 Python의 패키지 경로·호스트 비밀값을 자식 시험에 전달하지 않는다.
            env={"PATH": os.defpath, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        values.append(result.stdout.strip())
    assert values[0] == values[1]
