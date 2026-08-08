UPDATE subtitle_tracks
SET translated_text = normalized_text,
    translated_language_code = language_code,
    translation_metadata = translation_metadata || jsonb_build_object(
      'mode', 'copied_chinese_source',
      'migration', '012_backfill_chinese_translations'
    ),
    updated_at = now()
WHERE NULLIF(btrim(normalized_text), '') IS NOT NULL
  AND (
    lower(language_code) = 'zh'
    OR lower(language_code) LIKE 'zh-%'
  )
  AND translated_text IS NULL
  AND translated_language_code IS NULL;
