# Audio ingestion

```bash
make eval-audio
```

The pipeline's input assumption used to be text. Everything downstream of
speaker attribution — extraction, corroboration, the distinct-customer
count that decides publication — rests on knowing who said what, and with
text fixtures that was assumed rather than established.

## Measured

```
transcript segments      9
word error rate          0.089
language detected        en (p=0.97)
diarization (dual-channel)  1.000 (9/9 segments)
diarization (mono clustering) 0.667 (6/9 segments)
```

The 8.9% word error rate is almost entirely formatting: the speaker says
"seven point two", `large-v3` writes "7.2". That is the *correct*
transcript for this pipeline, since entity resolution matches `7.2` against
the catalog — so the metric understates the result, and WER is computed
over normalised words precisely so it does not overstate it either.

## Diarization: read the channel, do not infer it

The first implementation clustered spectral features and reached **0.667 on
two speakers** — barely above chance. Adding pitch made no difference, and
measuring said why: both synthesised voices sit at 155–235 Hz, so F0 does
not separate them. The assumption that a male and a female voice differ
sharply in pitch was simply wrong for this audio.

The fix was not a better clusterer. **Contact centres record dual-channel**
— agent on one side, customer on the other — precisely because diarization
is unreliable, and where that recording exists, inferring the speaker from
the waveform is solving a problem the microphone already solved. Channel
energy gives **1.000**.

Clustering remains as the mono fallback, with its 0.667 documented rather
than hidden. A production mono path would use a trained speaker-embedding
model; the point of the number is that it says plainly when it should not
be trusted.

Channel dominance becomes `speaker_confidence`, so crosstalk and overtalk
push attribution confidence toward 0.5 — which is exactly the signal the
attribution rules already consume to discount a weakly-diarized turn
(`docs/ATTRIBUTION.md`).

## Language identification

Taken from the ASR pass rather than from the transcript afterwards.
Guessing a language from text discards the acoustic evidence that settles
it, and a multilingual call centre is the case that matters: a transcript
the extractor cannot reason about should be routed, not silently processed.
