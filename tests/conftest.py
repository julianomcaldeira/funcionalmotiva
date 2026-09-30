import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="lumos_tests_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "test.db")
os.environ["APP_PASSWORD"] = ""      # desliga a auth básica nos testes
os.environ["AI_API_KEY"] = ""        # cada teste configura o que precisa

import pytest
from fastapi.testclient import TestClient

from app.db import (Analysis, AssistantChat, AssistantSession, Attachment, ChatMessage,
                    Chunk, Decision, DecisionRevision, Document, DocumentVersion, Email,
                    SessionLocal, Setting, init_db)
from app.main import app
from app.retrieval import mark_dirty

init_db()

TABLES = [Analysis, Attachment, ChatMessage, AssistantChat, AssistantSession,
          DocumentVersion, DecisionRevision, Chunk, Email, Decision, Document, Setting]


@pytest.fixture(autouse=True)
def clean_db():
    yield
    with SessionLocal() as s:
        for model in TABLES:
            s.query(model).delete()
        s.commit()
    mark_dirty()


@pytest.fixture
def client():
    return TestClient(app)


def make_decision(payload=None):
    base = {"titulo": "Tracking mostra etapa aprovada", "modulo": "Tracking/Etapas",
            "regra": "Exibir a etapa conforme aprovado no Coupa.", "fonte": "E-mail da Juliana",
            "data_decisao": "28/07/2026", "aprovado_por": "", "status": "vigente", "notas": "",
            "substituida_por": None}
    base.update(payload or {})
    return base