import importlib

import pytest


def pacer():
    assert importlib.util.find_spec('app.jobs.supplier_job_pacing') is not None, 'supplier job pacer missing'
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
    value = importlib.import_module('app.jobs.supplier_job_pacing').SupplierJobPacer(60, clock=lambda:clock[0], sleep=sleep)
    return value, clock


def test_supplier_gap_is_preserved_across_stock_market_macro_jobs():
    value, clock = pacer()
    observed = []
    for _ in ('stocks','market','macro'):
        with value.scope():
            observed.append(clock[0])
    assert observed == [0.0,1.0,2.0]


def test_nested_startup_is_reentrant_and_errors_still_keep_gap():
    value, clock = pacer()
    with pytest.raises(ValueError):
        with value.scope():
            with value.scope():
                assert clock[0] == 0
            raise ValueError('controlled')
    with value.scope():
        assert clock[0] == 1


def test_elapsed_time_is_not_slept_twice():
    value, clock = pacer()
    with value.scope():
        clock[0] = 3
    clock[0] = 5
    with value.scope():
        assert clock[0] == 5
