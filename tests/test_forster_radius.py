"""compute_forster_radius: units (nm, not Å), equation, no scipy."""

import numpy as np

from mmfdb.models import compute_forster_radius


def test_flat_acceptor_gives_the_closed_form_r0_in_nm():
    # With ε_A constant, J = ε ∫ f_D λ⁴ dλ / ∫ f_D dλ; a narrow donor at 520 nm
    # makes that ε·520⁴ to the grid's accuracy.
    wl = np.linspace(400.0, 700.0, 3001)
    donor = np.exp(-0.5 * ((wl - 520.0) / 2.0) ** 2)
    eps = np.full_like(wl, 1.0e5)
    r0, j = compute_forster_radius((wl, donor), (wl, eps), 0.9,
                                   kappa_squared=2.0 / 3.0, refractive_index=1.4)
    j_expected = 1.0e5 * 520.0**4
    assert abs(j / j_expected - 1.0) < 1e-3
    r0_expected = 0.02108 * (2.0 / 3.0 * 0.9 * 1.4**-4 * j_expected) ** (1.0 / 6.0)
    assert abs(r0 - r0_expected) < 1e-3 * r0_expected
    assert 5.0 < r0 < 12.0  # a FRET pair's R0 in nm, not the Å value (~10x)


def test_unsorted_spectra_and_partial_overlap():
    wl = np.linspace(450.0, 650.0, 401)
    donor = np.exp(-0.5 * ((wl - 520.0) / 15.0) ** 2)
    acc_wl = np.linspace(500.0, 700.0, 401)
    eps = 8.0e4 * np.exp(-0.5 * ((acc_wl - 560.0) / 20.0) ** 2)
    a = compute_forster_radius((wl, donor), (acc_wl, eps), 0.8)
    b = compute_forster_radius((wl[::-1], donor[::-1]), (acc_wl[::-1], eps[::-1]), 0.8)
    assert np.allclose(a, b)
    assert a[0] > 0 and a[1] > 0


def test_disjoint_spectra_give_zero():
    wl = np.linspace(400.0, 450.0, 51)
    assert compute_forster_radius((wl, np.ones_like(wl)), (wl + 100.0, np.ones_like(wl)), 0.5) == (0.0, 0.0)


def test_module_does_not_import_scipy():
    import ast
    import inspect

    import mmfdb.models as m

    tree = ast.parse(inspect.getsource(m))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(x.split(".")[0] == "scipy" for x in names)


def test_admin_fret_pair_stores_angstrom():
    from mmfdb.admin.backend.services import _forster_radius_angstrom

    assert _forster_radius_angstrom({"forster_radius": 52.0}) == 52.0
    assert _forster_radius_angstrom({"forster_radius_nm": 5.2}) == 52.0
    assert _forster_radius_angstrom({}) == 50.0  # the dictionary default
