from pathlib import Path

from app import research_runtime


def test_v940_railway_uses_research_safe_gunicorn_runtime():
    text = Path('railway.json').read_text(encoding='utf-8')
    assert '--worker-class gthread' in text
    assert '--threads 4' in text
    assert '--timeout 600' in text
    assert 'MALLOC_ARENA_MAX=2' in text
    assert 'MALLOC_TRIM_THRESHOLD_=65536' in text


def test_v940_release_memory_pressure_runs_gc_and_linux_trim(monkeypatch):
    calls = {'gc': 0, 'trim': 0}

    monkeypatch.setattr(research_runtime.gc, 'collect', lambda: calls.__setitem__('gc', calls['gc'] + 1))

    class FakeLibc:
        def malloc_trim(self, pad):
            assert pad == 0
            calls['trim'] += 1
            return 1

    monkeypatch.setattr(research_runtime, '_load_libc', lambda: FakeLibc())
    research_runtime.release_memory_pressure()

    assert calls == {'gc': 1, 'trim': 1}
