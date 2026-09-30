"""Banco de dados: e-mails, análises, registro de decisões e base de conhecimento."""
import os
import sys
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer,
                        LargeBinary, String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./funcional.db")
# O Render entrega "postgres://", o SQLAlchemy exige "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_USING_SQLITE = DATABASE_URL.startswith("sqlite")
if _USING_SQLITE and os.getenv("RENDER_SERVICE_NAME"):
    print("⚠️ ATENÇÃO: DATABASE_URL não configurado. Usando SQLite local. Dados NÃO persistem no Render.", file=sys.stderr)

connect_args = {"check_same_thread": False} if _USING_SQLITE else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def now():
    return datetime.utcnow()


class Email(Base):
    __tablename__ = "emails"
    id = Column(Integer, primary_key=True)
    message_id = Column(String(500), unique=True, index=True)
    thread_key = Column(String(500), index=True)
    folder = Column(String(100))
    direction = Column(String(10))  # "in" (recebido) | "out" (enviado pela StartGi)
    from_addr = Column(String(500))
    to_addr = Column(Text)
    cc_addr = Column(Text)
    subject = Column(String(1000))
    date = Column(DateTime, index=True)
    body = Column(Text)          # corpo completo
    body_clean = Column(Text)    # corpo sem o histórico citado
    is_motiva = Column(Boolean, default=False, index=True)
    status = Column(String(20), default="novo", index=True)  # novo | analisado | respondido | ignorado
    created_at = Column(DateTime, default=now)
    analyses = relationship("Analysis", back_populates="email", order_by="Analysis.id")
    attachments = relationship("Attachment", back_populates="email", order_by="Attachment.id",
                               cascade="all, delete-orphan")


class Analysis(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True)
    email_id = Column(Integer, ForeignKey("emails.id"), index=True)
    briefing_json = Column(Text)   # briefing estruturado (JSON)
    draft_subject = Column(String(1000))
    draft_body = Column(Text)
    final_body = Column(Text)      # o que foi de fato enviado (aprendizado de estilo)
    model = Column(String(200))
    sources_json = Column(Text)    # fontes usadas no contexto
    created_at = Column(DateTime, default=now)
    email = relationship("Email", back_populates="analyses")


class Decision(Base):
    """Registro de decisões: a regra vigente de cada assunto, com fonte e aprovador."""
    __tablename__ = "decisions"
    id = Column(Integer, primary_key=True)
    titulo = Column(String(500))
    modulo = Column(String(200))           # ex.: Tracking/Etapas, LOF, Tabela Comparativa, Coupa
    regra = Column(Text)
    fonte = Column(Text)                   # e-mail, EF, documento (com data)
    data_decisao = Column(String(50))
    aprovado_por = Column(String(300))     # quem aprovou do lado da Motiva
    status = Column(String(30), default="proposta", index=True)  # proposta | vigente | em_discussao | substituida | descartada
    substituida_por = Column(Integer, nullable=True)
    notas = Column(Text)
    origem_email_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    nome = Column(String(500))
    tipo = Column(String(50))   # especificacao | documentacao | whatsapp | validador | outro | regras
    content = Column(Text)
    file_data = Column(LargeBinary)   # arquivo original (ex.: PDF) para preview renderizado
    file_name = Column(String(500))   # nome/original do arquivo anexado
    tags = Column(String(300), default="")   # tags separadas por vírgula para filtrar e buscar
    versao = Column(Integer, default=1)      # nº da versão vigente
    created_at = Column(DateTime, default=now)


class Attachment(Base):
    """Anexo de e-mail: arquivo original + texto extraído (vira contexto e busca)."""
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True)
    email_id = Column(Integer, ForeignKey("emails.id"), index=True)
    filename = Column(String(500))
    content_type = Column(String(200))
    size = Column(Integer)
    file_data = Column(LargeBinary)
    extracted_text = Column(Text)
    created_at = Column(DateTime, default=now)
    email = relationship("Email", back_populates="attachments")


class Chunk(Base):
    """Trechos indexados para busca (documentos e e-mails)."""
    __tablename__ = "chunks"
    id = Column(Integer, primary_key=True)
    source_kind = Column(String(20), index=True)  # email | document
    source_id = Column(Integer, index=True)
    label = Column(String(1000))
    text = Column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)


class ChatMessage(Base):
    """Conversa do funcional com a IA sobre uma cadeia de e-mails (uma por cadeia)."""
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True)
    thread_key = Column(String(500), index=True)
    role = Column(String(20))          # user | assistant
    content = Column(Text)
    sources_json = Column(Text)
    created_at = Column(DateTime, default=now)


class AssistantSession(Base):
    """Uma conversa com o assistente global (tela Assistente), salva no histórico."""
    __tablename__ = "assistant_sessions"
    id = Column(Integer, primary_key=True)
    titulo = Column(String(500))       # primeira pergunta, truncada
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)
    messages = relationship("AssistantChat", back_populates="session",
                            order_by="AssistantChat.id", cascade="all, delete-orphan")


class AssistantChat(Base):
    """Troca de mensagens de uma conversa do assistente global."""
    __tablename__ = "assistant_chat"
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("assistant_sessions.id"), index=True)
    role = Column(String(20))          # user | assistant
    content = Column(Text)
    sources_json = Column(Text)        # fontes citadas pela resposta
    created_at = Column(DateTime, default=now)
    session = relationship("AssistantSession", back_populates="messages")


class DecisionRevision(Base):
    """Conteúdo anterior de uma decisão a cada edição (auditoria/histórico)."""
    __tablename__ = "decision_revisions"
    id = Column(Integer, primary_key=True)
    decision_id = Column(Integer, ForeignKey("decisions.id"), index=True)
    titulo = Column(String(500))
    modulo = Column(String(200))
    regra = Column(Text)
    fonte = Column(Text)
    data_decisao = Column(String(50))
    aprovado_por = Column(String(300))
    status = Column(String(30))
    substituida_por = Column(Integer, nullable=True)
    notas = Column(Text)
    created_at = Column(DateTime, default=now)


class DocumentVersion(Base):
    """Estado histórico (snapshot) de um documento da base, com número de versão."""
    __tablename__ = "document_versions"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), index=True)
    versao = Column(Integer)
    nome = Column(String(500))
    tipo = Column(String(50))
    content = Column(Text)
    file_data = Column(LargeBinary)
    file_name = Column(String(500))
    tags = Column(String(300), default="")
    nota = Column(String(1000))        # descrição da mudança nesta versão
    created_at = Column(DateTime, default=now)


def init_db():
    Base.metadata.create_all(engine)
    _ensure_columns()


def _ensure_columns():
    """Adiciona colunas novas em tabelas já existentes (create_all não altera tabela existente)."""
    from sqlalchemy import inspect, text
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("documents")}
    except Exception:
        return
    adds = []
    if "file_data" not in cols:
        t = "BYTEA" if engine.dialect.name == "postgresql" else "BLOB"
        adds.append(f"ALTER TABLE documents ADD COLUMN file_data {t}")
    if "file_name" not in cols:
        adds.append("ALTER TABLE documents ADD COLUMN file_name VARCHAR(500)")
    if "tags" not in cols:
        adds.append("ALTER TABLE documents ADD COLUMN tags VARCHAR(300)")
    if "versao" not in cols:
        adds.append("ALTER TABLE documents ADD COLUMN versao INTEGER")
    if adds:
        with engine.begin() as conn:
            for a in adds:
                conn.execute(text(a))


def get_setting(session, key, default=None):
    s = session.get(Setting, key)
    return s.value if s else default


def set_setting(session, key, value):
    s = session.get(Setting, key)
    if s:
        s.value = value
    else:
        session.add(Setting(key=key, value=value))
    session.commit()
