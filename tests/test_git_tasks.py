import asyncio
from pathlib import Path

import pytest
from helpers import git, make_repo

from codex_telegram_bot import git_tasks


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)

def test_slugify():
    assert git_tasks.slugify("Добавить тесты для parse_config!") == "dobavit-testy-dlya-parse-config"
    assert git_tasks.slugify("   ") == "task"
    assert len(git_tasks.slugify("слово " * 30)) <= 40
    assert git_tasks.slugify("Fix bug #42 in API") == "fix-bug-42-in-api"


def test_make_branch_name():
    name = git_tasks.make_branch_name("tg/", "Починить баг")
    assert name.startswith("tg/") and name.endswith("-pochinit-bag")


def test_prepare_task_branch(repo):
    base = asyncio.run(git_tasks.prepare_task_branch(repo, "tg/test-branch"))
    assert base == "main"
    assert asyncio.run(git_tasks.current_branch(repo)) == "tg/test-branch"
    assert asyncio.run(git_tasks.remote_url(repo)).endswith("remote.git")


def test_prepare_task_branch_requires_clean_tree(repo):
    (repo / "dirty.txt").write_text("x")
    with pytest.raises(git_tasks.GitError, match="не чистое"):
        asyncio.run(git_tasks.prepare_task_branch(repo, "tg/other"))


def test_prepare_task_branch_explicit_base(repo):
    git("checkout", "-b", "develop", cwd=repo)
    git("checkout", "main", cwd=repo)
    assert asyncio.run(git_tasks.prepare_task_branch(repo, "tg/from-develop", base="develop")) == "develop"
    assert asyncio.run(git_tasks.current_branch(repo)) == "tg/from-develop"


def test_default_branch_detection(repo):
    assert asyncio.run(git_tasks.default_branch(repo)) == "main"


def test_is_git_repo(repo, tmp_path):
    assert asyncio.run(git_tasks.is_git_repo(repo))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not asyncio.run(git_tasks.is_git_repo(plain))
    assert not asyncio.run(git_tasks.is_git_repo(tmp_path / "missing"))


def test_recent_commits(repo):
    asyncio.run(git_tasks.prepare_task_branch(repo, "tg/commits"))
    (repo / "new.txt").write_text("new\n")
    git("add", "new.txt", cwd=repo)
    git("commit", "-m", "add new file", cwd=repo)
    log = asyncio.run(git_tasks.recent_commits(repo, "main", "tg/commits"))
    assert "add new file" in log and "init" not in log


def test_run_cmd_timeout(tmp_path):
    code, out = asyncio.run(git_tasks.run_cmd(["sleep", "5"], tmp_path, timeout=0.5))
    assert code == 124 and "прервана" in out


def test_build_task_prompt():
    prompt = git_tasks.build_task_prompt(
        "сделай X", repo=Path("/r"), branch="tg/b", base="main", remote="git@github.com:o/r.git", auto_pr=True
    )
    assert "git push -u origin tg/b" in prompt
    assert "gh pr create --base main" in prompt
    assert "сделай X" in prompt
    no_pr = git_tasks.build_task_prompt("y", repo=Path("/r"), branch="b", base="main", remote=None, auto_pr=False)
    assert "no remote" in no_pr and "Do not create a pull request" in no_pr
