-- Review facts retain complete immutable original JSON and append-only decisions.
-- Their source identity outlives replacement of an unpublished draft projection.
ALTER TABLE review_facts DROP CONSTRAINT IF EXISTS review_facts_source_chunk_id_fkey;
