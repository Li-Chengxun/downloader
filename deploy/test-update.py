"""Exercise the packaged installer with mocked Docker; never touch real services."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "douyin-downloader-update-20261008-r2"
ARCHIVE = ROOT / (PACKAGE_NAME + ".tar.gz")
BASH = shutil.which("bash") if os.name != "nt" else "D:/Git/bin/bash.exe"
MOCK = r'''docker() {
  printf '%s\n' "$*" >> "$MOCK_LOG"
  case "$*" in
    'compose ps -q web') echo old-container ;;
    'inspect --format {{.Config.Image}} old-container') echo custom/downloader:stable ;;
    'inspect --format {{.Image}} old-container') echo sha256:old-image ;;
    'compose --profile proxy ps -q nginx')
      [[ "$SCENARIO" != proxy* ]] || echo nginx-container ;;
    'tag douyin-downloader:backup-'*) touch "$MOCK_ROLLED" ;;
    'compose build '*) [[ "$SCENARIO" != buildfail ]] || return 1 ;;
    'compose run '*) [[ "$SCENARIO" != preflightfail ]] || return 1 ;;
    'compose up -d --no-deps --force-recreate web')
      [[ "$SCENARIO" != startfail || -f "$MOCK_ROLLED" ]] || return 1 ;;
    'compose exec -T web python -c '*)
      if [[ "$*" == *style.css* ]]; then
        [[ "$SCENARIO" != cssfail ]] || return 1
      else
        [[ "$SCENARIO" != healthfail || -f "$MOCK_ROLLED" ]] || return 1
      fi ;;
    'exec nginx-container nginx -s reload')
      [[ "$SCENARIO" != proxyfail || -f "$MOCK_ROLLED" ]] || return 1 ;;
  esac
  return 0
}
sleep() { :; }
'''


def test() -> None:
    # Work only in an isolated, automatically removed directory inside the workspace.
    with tempfile.TemporaryDirectory(prefix=".update-test-", dir=ROOT) as tmp:
        base = Path(tmp)
        mock = base / "mock.sh"
        mock.write_text(MOCK, encoding="utf-8", newline="\n")
        scenarios = ("success", "cleansuccess", "proxysuccess", "buildfail", "preflightfail", "startfail", "healthfail", "cssfail", "proxyfail", "tampered")
        for scenario in scenarios:
            case = base / scenario
            case.mkdir()
            with tarfile.open(ARCHIVE) as archive:
                assert all(m.isfile() and ".." not in Path(m.name).parts for m in archive)
                archive.extractall(case)
            package = case / PACKAGE_NAME
            assert "BUILD_ARGS=()" not in (package / "deploy/update.sh").read_text(encoding="utf-8")
            target = case / "old project"
            (target / "backend").mkdir(parents=True)
            (target / "deploy/certs").mkdir(parents=True)
            protected = {".env": "BILI_COOKIE=private-test-cookie\n", "docker-compose.yml": "original-custom-compose\n", "deploy/nginx.conf": "original-domain-and-tls\n", "deploy/certs/cert.pem": "original-test-cert\n"}
            for name, data in protected.items():
                (target / name).write_text(data, encoding="utf-8", newline="\n")
            (target / "backend/main.py").write_text("# old backend\n", encoding="utf-8", newline="\n")
            if scenario == "tampered":
                (package / "backend/main.py").write_text("# damaged\n", encoding="utf-8", newline="\n")
            env = dict(os.environ, BASH_ENV=mock.as_posix(), MOCK_LOG=(case / "docker.log").as_posix(), MOCK_ROLLED=(case / "rolled").as_posix(), SCENARIO=scenario)
            args = [BASH, (package / "deploy/apply-update.sh").as_posix(), target.as_posix()]
            if scenario == "cleansuccess":
                args.append("--clean")
            result = subprocess.run(args, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            expected = 0 if scenario in ("success", "cleansuccess", "proxysuccess") else 1
            assert result.returncode == expected, (scenario, result.returncode, result.stdout, result.stderr)
            for name, data in protected.items():
                assert (target / name).read_text(encoding="utf-8") == data, (scenario, name)
            if scenario == "tampered":
                assert not (case / "docker.log").exists()
                assert not (target / ".update-backups").exists()
                assert (target / "backend/main.py").read_text() == "# old backend\n"
            else:
                assert (target / "backend/static/style.css").is_file()
                backup = next((target / ".update-backups").glob("code-*/before-update.tar.gz"))
                with tarfile.open(backup) as archive:
                    assert archive.extractfile("backend/main.py").read() == b"# old backend\n"
                    assert archive.extractfile(".env").read().decode() == protected[".env"]
                log = (case / "docker.log").read_text(encoding="utf-8")
                build_commands = [line for line in log.splitlines() if line.startswith("compose build ")]
                expected_build = "compose build --no-cache web" if scenario == "cleansuccess" else "compose build web"
                assert build_commands == [expected_build], (scenario, build_commands)
                assert "tag sha256:old-image douyin-downloader:backup-" in log
                assert (case / "rolled").exists() == (expected == 1)
                if scenario in ("buildfail", "preflightfail"):
                    assert "compose up" not in log
                if scenario in ("startfail", "healthfail", "cssfail", "proxyfail"):
                    assert log.count("compose up -d --no-deps --force-recreate web") == 2
                    assert "已恢复旧镜像" in result.stderr
                if scenario == "proxysuccess":
                    assert "exec nginx-container nginx -t" in log
                    assert "exec nginx-container nginx -s reload" in log
                assert "tag douyin-downloader:backup-" not in log or "custom/downloader:stable" in log
            print(f"PASS {scenario}: configuration preserved; expected update/rollback result")
    print("All 10 packaged installer scenarios passed (mocked Docker).")


def test_legacy_sed_fix() -> None:
    fixed = (ROOT / "deploy/update.sh").read_text(encoding="utf-8")
    legacy = fixed.replace("BUILD_ARGS=(web)", "BUILD_ARGS=()")
    legacy = legacy.replace("BUILD_ARGS=(--no-cache web)", "BUILD_ARGS=(--no-cache)")
    legacy = legacy.replace('build "${BUILD_ARGS[@]}"', 'build "${BUILD_ARGS[@]}" web')
    command = r'''sed -i.bak \
      -e 's/^BUILD_ARGS=()/BUILD_ARGS=(web)/' \
      -e 's/BUILD_ARGS=(--no-cache)/BUILD_ARGS=(--no-cache web)/' \
      -e 's/build "${BUILD_ARGS\[@\]}" web/build "${BUILD_ARGS[@]}"/' "$1"'''
    with tempfile.TemporaryDirectory(prefix=".update-test-", dir=ROOT) as tmp:
        path = Path(tmp) / "old update.sh"
        path.write_text(legacy, encoding="utf-8", newline="\n")
        result = subprocess.run([BASH, "-c", command, "legacy-fix", path.as_posix()], capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert path.read_text(encoding="utf-8") == fixed
        assert path.with_name(path.name + ".bak").read_text(encoding="utf-8") == legacy
    print("PASS server hotfix: updates all three lines and preserves original script backup")


if __name__ == "__main__":
    test()
    test_legacy_sed_fix()
