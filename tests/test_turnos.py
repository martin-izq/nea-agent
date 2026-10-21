"""039 — Turnos en vivo: Nea saca turno para hoy, dice cuánto falta y cancela
contra `/api/bot/turnos` de vocero-crm.

Las herramientas existen solo con la clave «turnos» EXPLÍCITA en el perfil:
un CRM sin la bandera (o anterior) no la manda y no tiene la ruta. Los números
(cuántos antes, hora estimada) los calcula el CRM; Nea los pasa tal cual.
"""
from __future__ import annotations

import json

import httpx

from app.llm import LlmReply, ToolCall
from app.main import create_app
from app.profile import BusinessProfile
from app.tools import CLAVE_DE_HERRAMIENTA, turnos_encendidos
from tests.conftest import CRM_CONV_ID, CRM_URL, despachar, make_ctx, mock_crm_basics

ESTADO = {
    "timezone": "America/Argentina/Buenos_Aires",
    "now": "15:00",
    "agendas": [{"id": "qa_1", "name": "Clínica general", "walkIn": True, "peopleWaiting": 3, "waitMinutes": 45}],
    "turnos": [],
}
TURNO = {"id": "tn_1", "code": "A-4", "peopleBefore": 3, "estimatedAt": "15:45", "link": "http://crm.test/t/tn_1.x"}


class _Perfil:
    def __init__(self, perfil: BusinessProfile) -> None:
        self._p = perfil

    async def get(self) -> BusinessProfile:
        return self._p


def _nombres(schemas):
    return {t["function"]["name"] for t in schemas or []}


async def _turno(ctx, texto="hola, quiero atenderme hoy en clínica"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(ctx=ctx)), base_url="http://bot.test"
    ) as client:
        await despachar(client, ctx, esperar=True, text=texto)


def test_las_herramientas_son_de_la_clave_turnos():
    for nombre in ("queue_status", "queue_slots", "join_queue", "cancel_turn", "confirm_coming"):
        assert CLAVE_DE_HERRAMIENTA[nombre] == "turnos"


def test_solo_con_la_clave_explicita():
    assert turnos_encendidos(BusinessProfile(tools=frozenset({"turnos", "escalar"})))
    # Sin `tools` (CRM viejo) se encienden todas las demás, pero turnos no:
    # del otro lado puede no haber `/api/bot/turnos`.
    assert not turnos_encendidos(BusinessProfile(tools=None))
    assert not turnos_encendidos(BusinessProfile(tools=frozenset({"agendar"})))


async def test_sin_la_clave_no_se_ensenan(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Turnera", tools=frozenset({"ficha", "escalar"})))
    mock_crm_basics(respx_mock)
    ctx.llm.replies = [LlmReply(content="¡Hola!")]
    await _turno(ctx)
    assert not _nombres(ctx.llm.calls[0]["tools"]) & {"queue_status", "join_queue"}
    assert "TURNOS:" not in ctx.llm.calls[0]["messages"][0]["content"]


async def test_consulta_y_saca_turno_con_los_numeros_del_crm(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Turnera", tools=frozenset({"turnos", "escalar"})))
    mock_crm_basics(respx_mock)
    estado = respx_mock.get(f"{CRM_URL}/api/bot/turnos").mock(return_value=httpx.Response(200, json=ESTADO))
    alta = respx_mock.post(f"{CRM_URL}/api/bot/turnos").mock(
        return_value=httpx.Response(201, json={"turno": TURNO, "now": "15:00"})
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="q", name="queue_status", arguments={})]),
        LlmReply(content=None, tool_calls=[ToolCall(id="j", name="join_queue", arguments={"agenda_id": "qa_1", "nombre": "Ana"})]),
        LlmReply(content="Listo, sos el A-4: hay 3 antes, te atienden ≈ 15:45. Link: http://crm.test/t/tn_1.x"),
    ]
    await _turno(ctx)

    primera = ctx.llm.calls[0]
    assert {"queue_status", "queue_slots", "join_queue", "cancel_turn", "confirm_coming"} <= _nombres(primera["tools"])
    assert "TURNOS:" in primera["messages"][0]["content"]
    assert estado.calls.last.request.url.params["conversationId"] == CRM_CONV_ID
    assert json.loads(alta.calls.last.request.content) == {
        "conversationId": CRM_CONV_ID, "agendaId": "qa_1", "name": "Ana",
    }
    # El resultado del alta llega al modelo con los números del CRM.
    resultado = json.loads(ctx.llm.calls[2]["messages"][-1]["content"])
    assert resultado["ok"] is True and resultado["turno"]["code"] == "A-4"
    assert "pásale el código" in resultado["nota"]
    # El link no llega al modelo: no lo puede pegar.
    assert "link" not in resultado["turno"]


async def test_con_bienvenida_del_crm_no_repite_los_datos(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Turnera", tools=frozenset({"turnos", "escalar"})))
    mock_crm_basics(respx_mock)
    respx_mock.post(f"{CRM_URL}/api/bot/turnos").mock(
        return_value=httpx.Response(201, json={"turno": TURNO, "now": "15:00", "bienvenida": True})
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="j", name="join_queue", arguments={"agenda_id": "qa_1"})]),
        LlmReply(content="¡Listo! ¿Algo más?"),
    ]
    await _turno(ctx)
    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert "NO lo repitas" in resultado["nota"]


async def test_reserva_con_hora_de_queue_slots(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Turnera", tools=frozenset({"turnos", "escalar"})))
    mock_crm_basics(respx_mock)
    huecos = respx_mock.get(f"{CRM_URL}/api/bot/turnos/huecos").mock(
        return_value=httpx.Response(200, json={"agenda": {"id": "qa_dj"}, "date": "2026-10-23", "huecos": [
            {"startUtc": "2026-10-23T13:00:00.000Z", "day": "2026-10-23", "dayLabel": "viernes 23/10", "time": "10:00"},
        ]})
    )
    alta = respx_mock.post(f"{CRM_URL}/api/bot/turnos").mock(
        return_value=httpx.Response(201, json={"turno": {"id": "tn_9", "code": "DJ-1", "time": "10:00", "link": "x"}, "bienvenida": True})
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="h", name="queue_slots", arguments={"agenda_id": "qa_dj", "fecha": "2026-10-23"})]),
        LlmReply(content=None, tool_calls=[ToolCall(id="j", name="join_queue", arguments={"agenda_id": "qa_dj", "start_utc": "2026-10-23T13:00:00.000Z"})]),
        LlmReply(content="¡Listo!"),
    ]
    await _turno(ctx, "quiero turno con el dr juarez el viernes a las 10")
    assert huecos.calls.last.request.url.params["date"] == "2026-10-23"
    assert json.loads(alta.calls.last.request.content)["startUtc"] == "2026-10-23T13:00:00.000Z"


async def test_un_409_vuelve_con_su_mensaje_y_el_turno_que_ya_tiene(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Turnera", tools=frozenset({"turnos", "escalar"})))
    mock_crm_basics(respx_mock)
    respx_mock.post(f"{CRM_URL}/api/bot/turnos").mock(
        return_value=httpx.Response(409, json={"error": {"code": "ya_tiene_turno", "message": "Ya tenés un turno hoy en esta agenda."}})
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="j", name="join_queue", arguments={"agenda_id": "qa_1"})]),
        LlmReply(content="Ya tenés turno hoy."),
    ]
    await _turno(ctx)
    resultado = json.loads(ctx.llm.calls[1]["messages"][-1]["content"])
    assert resultado == {"ok": False, "error": "ya_tiene_turno", "detalle": "Ya tenés un turno hoy en esta agenda."}


async def test_cancelar_exige_confirmacion(respx_mock):
    ctx = make_ctx()
    ctx.profile = _Perfil(BusinessProfile(agent_name="Turnera", tools=frozenset({"turnos", "escalar"})))
    mock_crm_basics(respx_mock)
    cancelar = respx_mock.post(f"{CRM_URL}/api/bot/turnos/tn_1").mock(
        return_value=httpx.Response(200, json={"ok": True, "status": "cancelled"})
    )
    ctx.llm.replies = [
        LlmReply(content=None, tool_calls=[ToolCall(id="c", name="cancel_turn", arguments={"turn_id": "tn_1", "confirmation": False})]),
        LlmReply(content=None, tool_calls=[ToolCall(id="d", name="cancel_turn", arguments={"turn_id": "tn_1", "confirmation": True})]),
        LlmReply(content="Listo, cancelado."),
    ]
    await _turno(ctx, "sí, cancelalo")
    assert cancelar.call_count == 1
    assert json.loads(cancelar.calls.last.request.content) == {"conversationId": CRM_CONV_ID, "action": "cancel"}
