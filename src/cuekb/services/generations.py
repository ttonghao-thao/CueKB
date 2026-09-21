"""Worker execution for one bounded-attempt, paginated generation rebuild."""

from cuekb.adapters.generations import MaintenanceBusy
from cuekb.adapters.model_client import HttpModelClient
from cuekb.adapters.opensearch import OpenSearchBackend


def backend(settings, row):
    return OpenSearchBackend(
        settings.opensearch_url,
        row["index_name"],
        row["dimension"],
        row["embedding_model"],
        settings.opensearch_timeout_ms,
    )


def validate_generation(settings, row):
    search = backend(settings, row)
    try:
        if not search.client.indices.exists(index=search.index_name):
            raise ValueError("generation_index_missing")
        search.validate_existing_index()
        if search.client.count(index=search.index_name)["count"] != row["chunk_count"]:
            raise ValueError("generation_chunk_count_mismatch")
    finally:
        search.client.close()


def rebuild_once(repo, settings):
    try:
        with repo.maintenance_guard(exclusive=True):
            row = repo.pending_generation(settings.worker_max_attempts)
            if row is None:
                return False
            search = backend(settings, row)
            config = row["config"]
            model = HttpModelClient(
                config["embedding_base_url"],
                config["embedding_api_key"],
                config["embedding_model"],
                config["reranker_base_url"],
                config["reranker_api_key"] or "",
                config["reranker_model"],
            )
            try:
                # This is never an active index. Restart after a crashed build is deterministic.
                if search.client.indices.exists(index=search.index_name):
                    search.client.indices.delete(index=search.index_name)
                search.ensure_index()
                revisions = repo.generation_revisions()
                after = None
                count = 0
                while chunks := repo.generation_page(after, 32):
                    vectors = model.embed(
                        [c.search_text for c in chunks], settings.worker_model_timeout_ms
                    )
                    search.index(chunks, vectors)
                    docs = search.client.mget(
                        index=search.index_name, body={"ids": [str(c.id) for c in chunks]}
                    )["docs"]
                    if len(docs) != len(chunks) or any(
                        not d.get("found") or d["_source"]["search_text"] != c.search_text
                        for d, c in zip(docs, chunks, strict=True)
                    ):
                        raise ValueError("generation_content_verification_failed")
                    count += len(chunks)
                    after = chunks[-1].id
                search.client.indices.refresh(index=search.index_name)
                if search.client.count(index=search.index_name)["count"] != count:
                    raise ValueError("generation_chunk_count_mismatch")
                repo.generation_ready(row["id"], count, revisions)
            except Exception:
                repo.generation_failed(row["id"], settings.worker_max_attempts)
                # Deliberately persist a safe code, never provider messages/credentials.
            finally:
                search.client.close()
                model.close()
            return True
    except MaintenanceBusy:
        return False
