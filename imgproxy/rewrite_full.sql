-- ============================================================
-- immortal 图片 URL 本地化改写（全量模式，仅首次部署时手动执行一次）
-- 之后由 imgrewrite 容器的 rewrite.sql 增量维护
-- ============================================================

UPDATE article
SET preview_images = ARRAY(
    SELECT CASE
        WHEN x LIKE 'http://192.168.2.70:8002/%' THEN x
        WHEN x ~ '^https?://[^/]+/' THEN regexp_replace(x, '^https?://', 'http://192.168.2.70:8002/')
        ELSE x
    END
    FROM unnest(preview_images) x
)
WHERE EXISTS (
    SELECT 1 FROM unnest(preview_images) x
    WHERE x ~ '^https?://' AND x NOT LIKE 'http://192.168.2.70:8002/%'
);

UPDATE movie
SET poster = CASE
        WHEN poster LIKE 'http://192.168.2.70:8002/%' THEN poster
        WHEN poster ~ '^https?://[^/]+/' THEN regexp_replace(poster, '^https?://', 'http://192.168.2.70:8002/')
        ELSE poster
    END,
    banner = CASE
        WHEN banner LIKE 'http://192.168.2.70:8002/%' THEN banner
        WHEN banner ~ '^https?://[^/]+/' THEN regexp_replace(banner, '^https?://', 'http://192.168.2.70:8002/')
        ELSE banner
    END,
    still_photos = ARRAY(
        SELECT CASE
            WHEN x LIKE 'http://192.168.2.70:8002/%' THEN x
            WHEN x ~ '^https?://[^/]+/' THEN regexp_replace(x, '^https?://', 'http://192.168.2.70:8002/')
            ELSE x
        END
        FROM unnest(still_photos) x
    )
WHERE poster ~ '^https?://[^/]+/' OR banner ~ '^https?://[^/]+/'
   OR EXISTS (SELECT 1 FROM unnest(still_photos) x
              WHERE x ~ '^https?://' AND x NOT LIKE 'http://192.168.2.70:8002/%');

-- 残留统计（正常应趋近 0 行）
SELECT 'article残留' AS k, count(*) AS n
FROM (SELECT unnest(preview_images) AS x FROM article) t
WHERE x ~ '^https?://' AND x NOT LIKE 'http://192.168.2.70:8002/%'
UNION ALL
SELECT 'movie残留', count(*) FROM movie
WHERE poster ~ '^https?://' OR banner ~ '^https?://'
   OR EXISTS (SELECT 1 FROM unnest(still_photos) x WHERE x ~ '^https?://');
