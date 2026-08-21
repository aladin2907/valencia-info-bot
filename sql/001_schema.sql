-- Valencia Info Bot — схема базы знаний
-- Postgres 17 + pgvector. Запускается автоматически при первом старте контейнера db.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Слой 1: сырой архив сообщений. Неприкосновенный источник правды.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    group_slug          text        NOT NULL,
    message_id          bigint      NOT NULL,
    sender_id           bigint,
    sender_name         text,
    text                text,
    entities            jsonb,
    reply_to_message_id bigint,
    sent_at             timestamptz NOT NULL,
    edited_at           timestamptz,
    tg_link             text,
    raw                 jsonb,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (group_slug, message_id)
);

CREATE INDEX IF NOT EXISTS messages_sent_at_idx ON messages (sent_at DESC);
CREATE INDEX IF NOT EXISTS messages_reply_to_idx ON messages (group_slug, reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Слой 2: треды — основная единица поиска.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS threads (
    id               bigserial PRIMARY KEY,
    group_slug       text        NOT NULL,
    root_message_id  bigint      NOT NULL,
    content          text        NOT NULL,
    message_count    int         NOT NULL DEFAULT 1,
    started_at       timestamptz NOT NULL,
    -- свежесть считается по последнему ответу, а не по началу треда
    last_activity_at timestamptz NOT NULL,
    content_hash     text        NOT NULL,
    tg_link          text,
    metadata         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- русский словарь для стемминга + simple для точных форм (украинского словаря нет)
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('russian', coalesce(content, '')), 'A') ||
        setweight(to_tsvector('simple',  coalesce(content, '')), 'B')
    ) STORED,
    UNIQUE (group_slug, root_message_id)
);

CREATE INDEX IF NOT EXISTS threads_tsv_idx ON threads USING gin (tsv);
CREATE INDEX IF NOT EXISTS threads_activity_idx ON threads (last_activity_at DESC);
CREATE INDEX IF NOT EXISTS threads_group_idx ON threads (group_slug);

-- Эмбеддинги вынесены отдельно: смена модели или сбой OpenAI не трогают данные,
-- строки со status='pending' просто пересчитываются следующей ночью.
CREATE TABLE IF NOT EXISTS thread_embeddings (
    thread_id     bigint      NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    model         text        NOT NULL DEFAULT 'bge-m3',
    dimensions    int         NOT NULL DEFAULT 1024,
    source_hash   text        NOT NULL,          -- от какого текста посчитан вектор
    embedding     vector(1024),
    status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'ready', 'failed')),
    attempt_count int         NOT NULL DEFAULT 0,
    last_error    text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, model)
);

CREATE INDEX IF NOT EXISTS thread_embeddings_hnsw_idx
    ON thread_embeddings USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS thread_embeddings_pending_idx
    ON thread_embeddings (status) WHERE status <> 'ready';

-- ---------------------------------------------------------------------------
-- Слой 3: факты. Две оси времени: когда факт верен в мире (valid_from/invalid_at)
-- и когда система о нём узнала (created_at). Ничего не удаляется — только гасится.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id                bigserial PRIMARY KEY,
    topic             text        NOT NULL,
    -- Ключ, внутри которого факты могут гасить друг друга: 'nie:tasa',
    -- 'empadronamiento:cita_wait', 'school:comedor_price'. NULL — факт ни с чем
    -- не конкурирует и просто сосуществует с другими (см. ARCHITECTURE.md §3.3).
    fact_key          text,
    statement         text        NOT NULL,
    embedding         vector(1024),
    valid_from        timestamptz NOT NULL,
    invalid_at        timestamptz,                  -- NULL = факт актуален
    superseded_by     bigint      REFERENCES facts(id) ON DELETE SET NULL,
    supersession_note text,
    confidence        real        NOT NULL DEFAULT 0.5,
    source_thread_ids bigint[]    NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS facts_hnsw_idx
    ON facts USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS facts_active_idx
    ON facts (topic, valid_from DESC) WHERE invalid_at IS NULL;
-- конкуренция фактов разрешается внутри одного ключа
CREATE INDEX IF NOT EXISTS facts_key_idx
    ON facts (fact_key, valid_from DESC) WHERE fact_key IS NOT NULL AND invalid_at IS NULL;

-- ---------------------------------------------------------------------------
-- Пользователи и rate limit (общие для Telegram и мобильного приложения)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                      bigserial PRIMARY KEY,
    platform                text        NOT NULL DEFAULT 'telegram',
    external_id             text        NOT NULL,
    username                text,
    first_name              text,
    last_name               text,
    language_code           text,
    message_count_today     int         NOT NULL DEFAULT 0,
    next_allowed_message_at timestamptz NOT NULL DEFAULT now(),
    last_interaction_at     timestamptz NOT NULL DEFAULT now(),
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, external_id)
);

-- ---------------------------------------------------------------------------
-- Журнал вопросов и ответов — нужен для оценки качества поиска
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_log (
    id             bigserial PRIMARY KEY,
    user_id        bigint REFERENCES users(id) ON DELETE SET NULL,
    question       text        NOT NULL,
    key_phrase     text,
    thread_ids     bigint[],
    answer         text,
    latency_ms     int,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
