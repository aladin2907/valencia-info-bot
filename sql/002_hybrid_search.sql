-- Гибридный поиск: векторный + полнотекстовый, слияние через RRF,
-- поверх — коэффициент свежести по дате последнего ответа в треде.
--
-- Основа — рецепт hybrid_search из документации Supabase, адаптированный:
--   * русский + simple словари вместо english;
--   * свежесть по last_activity_at;
--   * фильтр по группам и по датам.
-- Реранкер (Cohere) применяется уже в приложении, поверх выдачи этой функции.

CREATE OR REPLACE FUNCTION hybrid_search(
    query_text       text,
    query_embedding  vector(1536),
    match_count      int     DEFAULT 50,
    full_text_weight float   DEFAULT 1.0,
    semantic_weight  float   DEFAULT 1.0,
    rrf_k            int     DEFAULT 50,
    -- свежесть: 0.5 года = период полураспада; recency_floor не даёт
    -- старым тредам обнулиться (уникальный ответ 2024 года должен выживать)
    half_life_days   float   DEFAULT 365.0,
    recency_floor    float   DEFAULT 0.6,
    filter_groups    text[]  DEFAULT NULL,
    date_from        timestamptz DEFAULT NULL,
    date_to          timestamptz DEFAULT NULL
)
RETURNS TABLE (
    id               bigint,
    group_slug       text,
    content          text,
    last_activity_at timestamptz,
    tg_link          text,
    score            float,
    recency          float
)
LANGUAGE sql
AS $$
WITH
full_text AS (
    SELECT t.id,
           row_number() OVER (
               ORDER BY ts_rank_cd(t.tsv, websearch_to_tsquery('russian', query_text)) DESC
           ) AS rank_ix
    FROM threads t
    WHERE query_text IS NOT NULL
      AND t.tsv @@ websearch_to_tsquery('russian', query_text)
      AND (filter_groups IS NULL OR t.group_slug = ANY (filter_groups))
      AND (date_from IS NULL OR t.last_activity_at >= date_from)
      AND (date_to   IS NULL OR t.last_activity_at <= date_to)
    ORDER BY rank_ix
    LIMIT least(match_count, 30) * 2
),
semantic AS (
    SELECT t.id,
           row_number() OVER (ORDER BY e.embedding <=> query_embedding) AS rank_ix
    FROM threads t
    JOIN thread_embeddings e ON e.thread_id = t.id AND e.status = 'ready'
    WHERE query_embedding IS NOT NULL
      AND (filter_groups IS NULL OR t.group_slug = ANY (filter_groups))
      AND (date_from IS NULL OR t.last_activity_at >= date_from)
      AND (date_to   IS NULL OR t.last_activity_at <= date_to)
    ORDER BY rank_ix
    LIMIT least(match_count, 30) * 2
)
SELECT
    t.id,
    t.group_slug,
    t.content,
    t.last_activity_at,
    t.tg_link,
    (
        (coalesce(1.0 / (rrf_k + full_text.rank_ix), 0.0) * full_text_weight +
         coalesce(1.0 / (rrf_k + semantic.rank_ix),  0.0) * semantic_weight)
        * greatest(
            power(0.5, extract(epoch FROM (now() - t.last_activity_at)) / 86400.0 / half_life_days),
            recency_floor
          )
    )::float AS score,
    greatest(
        power(0.5, extract(epoch FROM (now() - t.last_activity_at)) / 86400.0 / half_life_days),
        recency_floor
    )::float AS recency
FROM full_text
FULL OUTER JOIN semantic ON full_text.id = semantic.id
JOIN threads t ON t.id = coalesce(full_text.id, semantic.id)
ORDER BY score DESC
LIMIT match_count;
$$;


-- Подбор актуальных фактов по теме вопроса.
-- Отдаёт только не погашенные факты (invalid_at IS NULL).
CREATE OR REPLACE FUNCTION match_facts(
    query_embedding vector(1536),
    match_count     int   DEFAULT 10,
    min_similarity  float DEFAULT 0.35,
    as_of           timestamptz DEFAULT NULL   -- «как было летом»: срез на дату
)
RETURNS TABLE (
    id                bigint,
    topic             text,
    statement         text,
    valid_from        timestamptz,
    source_thread_ids bigint[],
    similarity        float
)
LANGUAGE sql
AS $$
SELECT f.id,
       f.topic,
       f.statement,
       f.valid_from,
       f.source_thread_ids,
       (1 - (f.embedding <=> query_embedding))::float AS similarity
FROM facts f
WHERE f.embedding IS NOT NULL
  AND (1 - (f.embedding <=> query_embedding)) > min_similarity
  AND CASE
        WHEN as_of IS NULL THEN f.invalid_at IS NULL
        ELSE f.valid_from <= as_of
             AND (f.invalid_at IS NULL OR f.invalid_at > as_of)
      END
ORDER BY f.embedding <=> query_embedding
LIMIT match_count;
$$;
