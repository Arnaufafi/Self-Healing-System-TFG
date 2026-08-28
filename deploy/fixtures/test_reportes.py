"""Tests de CI del core bancario."""

from gestor_cuentas import CuentaBancaria


def test_generar_reporte_de_cuenta_nueva():
    cuenta = CuentaBancaria("CI-001", "Cuenta CI")
    reporte = cuenta.generar_reporte()
    assert reporte["titular"] == "Cuenta CI"
    assert reporte["saldo_actual"] == 0.0
    assert reporte["promedio_transacciones"] == 0.0
