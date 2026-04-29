"""KB ingestion source-runner modules.

Each kind of ``kb_source.kind`` is implemented by a module in this
package exposing a single ``run(ctx, *, session_factory, storage_backend)``
async entry point. The dispatcher in
:mod:`surogates.jobs.kb_ingest` looks up the runner by kind and invokes
it under a per-source advisory lock.

Step-4 runners:
  - ``markdown_dir`` — walk a local path or git repo, ingest .md files.
  - ``web_scraper`` — sitemap-driven HTTP fetch + markitdown conversion (4c).
  - ``file_upload`` — pull files from a Garage holding prefix + markitdown (4c).

All runners share the :class:`SourceContext` shape and the
:class:`IngestResult` return type defined in
:mod:`surogates.jobs.kb_sources._base`.
"""
