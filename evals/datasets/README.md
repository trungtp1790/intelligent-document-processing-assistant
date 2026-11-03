# AI evaluation corpus

`synthetic_invoice_corpus_v1/` is a labeled, synthetic extraction-evaluation corpus. It is separate from `data/demo/` and the 12 SHA-bound fixture examples. It contains 90 generated PDFs with programmatic reference labels, not human-annotated labels and not real vendor invoices.

The split is grouped by layout family and source document: 30 dev samples across 3 families, 20 validation samples across 2 families, and 40 held-out test samples across 4 families. Each family has 10 samples, and a family, source document, or document hash cannot cross splits. The manifest records a separate SHA-256-verified `label_path`, source/license-or-consent, synthetic status, label provenance, modality, language, PO reference, expected findings, and document hash.

Rebuild the synthetic corpus with a Unicode font available on the machine:

```powershell
python scripts/build_synthetic_ai_corpus.py --font C:\Windows\Fonts\arial.ttf
```

The AI runner validates the full manifest and input hashes, then evaluates only the requested split. It does not read fixture predictions or any post-review/manual-correction values. Rule regression on canonical reference labels is reported in its own section and is excluded from AI extraction accuracy.

Run the validation split with explicit live-evaluation intent:

```powershell
python scripts/evaluate_ai_corpus.py --split validation --live
```

Live requests require `EXTRACTION_PROVIDER=bedrock`, `ALLOW_LIVE_MODEL_CALLS=true`, `AWS_REGION`, `BEDROCK_MODEL_ID`, and valid AWS access. Without those settings, the report records blocked samples and leaves AI accuracy null. The held-out split additionally requires `--confirm-held-out`:

```powershell
python scripts/evaluate_ai_corpus.py --split test --confirm-held-out --live
```

Any test-split prediction consumes that held-out set; changes made after viewing it require a new test split before making an independent accuracy claim. Reports go to `evals/reports/`; raw prediction payloads go under ignored `var/eval_predictions/` and should remain private when evaluating non-synthetic documents.
