-- ============================================================
-- immortal 图片 URL 本地化改写（全量幂等，供 imgrewrite 每 5 分钟循环执行）
-- 作用：把抓取入库的远程图片 URL 前缀改写为本地缓存代理地址
--       https://host/path  ->  http://IMGPROXY_HOST:8002/host/path
-- 幂等：已改写前缀（http://IMGPROXY_HOST:8002/）保持不变，可重复执行
-- 全量：不依赖 update_time（存量该字段多为 NULL），EXISTS 过滤保证只更新
--       含未改写远程 URL 的行；immortal 无论 INSERT 还是刷新预览图，
--       5 分钟内都会被重新改写回本地地址
-- 说明：格式异常的 URL（如 https:////cdn... 分隔线、magnet: 磁力串）不匹配
--       改写正则，原样保留，浏览器加载失败即裂图，属历史脏数据不处理
-- ============================================================

UPDATE article
SET preview_images = ARRAY(
    SELECT CASE
        WHEN x LIKE 'http://IMGPROXY_HOST:8002/%' THEN x
        WHEN x ~ '^https?://[^/]+/' THEN regexp_replace(x, '^https?://', 'http://IMGPROXY_HOST:8002/')
        ELSE x
    END
    FROM unnest(preview_images) x
)
WHERE EXISTS (
    SELECT 1 FROM unnest(preview_images) x
    WHERE x ~ '^https?://[^/]+/' AND x NOT LIKE 'http://IMGPROXY_HOST:8002/%'
);

UPDATE movie
SET poster = CASE
        WHEN poster LIKE 'http://IMGPROXY_HOST:8002/%' THEN poster
        WHEN poster ~ '^https?://[^/]+/' THEN regexp_replace(poster, '^https?://', 'http://IMGPROXY_HOST:8002/')
        ELSE poster
    END,
    banner = CASE
        WHEN banner LIKE 'http://IMGPROXY_HOST:8002/%' THEN banner
        WHEN banner ~ '^https?://[^/]+/' THEN regexp_replace(banner, '^https?://', 'http://IMGPROXY_HOST:8002/')
        ELSE banner
    END,
    still_photos = ARRAY(
        SELECT CASE
            WHEN x LIKE 'http://IMGPROXY_HOST:8002/%' THEN x
            WHEN x ~ '^https?://[^/]+/' THEN regexp_replace(x, '^https?://', 'http://IMGPROXY_HOST:8002/')
            ELSE x
        END
        FROM unnest(still_photos) x
    )
WHERE (poster ~ '^https?://[^/]+/' AND poster NOT LIKE 'http://IMGPROXY_HOST:8002/%')
   OR (banner ~ '^https?://[^/]+/' AND banner NOT LIKE 'http://IMGPROXY_HOST:8002/%')
   OR EXISTS (SELECT 1 FROM unnest(still_photos) x
              WHERE x ~ '^https?://[^/]+/' AND x NOT LIKE 'http://IMGPROXY_HOST:8002/%');

-- 演员头像 / 写真（v5.4 新增）
-- cast.avatar 517 张全部来自 pics.dmm.co.jp，此前完全没纳入改写，浏览器直连 DMM
-- 必然失败 → 演员库、影片详情页的演员头像整片裂图。cast 是 SQL 保留字，必须带双引号。
UPDATE "cast"
SET avatar = CASE
        WHEN avatar LIKE 'http://IMGPROXY_HOST:8002/%' THEN avatar
        WHEN avatar ~ '^https?://[^/]+/' THEN regexp_replace(avatar, '^https?://', 'http://IMGPROXY_HOST:8002/')
        ELSE avatar
    END,
    photo = CASE
        WHEN photo LIKE 'http://IMGPROXY_HOST:8002/%' THEN photo
        WHEN photo ~ '^https?://[^/]+/' THEN regexp_replace(photo, '^https?://', 'http://IMGPROXY_HOST:8002/')
        ELSE photo
    END
WHERE (avatar ~ '^https?://[^/]+/' AND avatar NOT LIKE 'http://IMGPROXY_HOST:8002/%')
   OR (photo  ~ '^https?://[^/]+/' AND photo  NOT LIKE 'http://IMGPROXY_HOST:8002/%');
