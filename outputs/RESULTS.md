# Benchmark Results (RIOT OS ONLY)

This document tracks the results and step sizes for all models benchmarked on RIOT OS issues.
Every patch has been individually evaluated by the `validate_riot_instance.sh` script.

| Instance / PR | Model | Steps Taken | Agent Exit Status | Validation Result |
| :--- | :--- | :--- | :--- | :--- |
| **riot__riot-17607** | Deepseek-V3 | 50 | LimitsExceeded | **FAIL** |
| **riot__riot-17607** | Qwen3-Coder | 50 | LimitsExceeded | **FAIL** |
| **riot__riot-17607** | claude-opus-4-6 | 50 | LimitsExceeded | **FAIL** |
| **riot__riot-17607** | gemini-2.5-pro | 50 | LimitsExceeded | **FAIL** |
| **riot__riot-17607** | gpt-5.4 | 39 | Submitted | **FAIL** |
| **riot__riot-20197** | Deepseek-V3 | 33 | Submitted | **PASS** |
| **riot__riot-20197** | Qwen3-Coder | 50 | LimitsExceeded | **FAIL** |
| **riot__riot-20197** | claude-opus-4-6 | 26 | Submitted | **PASS** |
| **riot__riot-20197** | gemini-2.5-pro | 48 | Submitted | **PASS** |
| **riot__riot-20197** | gpt-5.4 | 8 | Submitted | **PASS** |
| **riot__riot-20857** | Deepseek-V3 | 21 | Submitted | **PASS** |
| **riot__riot-20857** | Qwen3-Coder | 13 | Submitted | **PASS** |
| **riot__riot-20857** | claude-opus-4-6 | 5 | Submitted | **PASS** |
| **riot__riot-20857** | gemini-2.5-pro | 18 | Submitted | **PASS** |
| **riot__riot-20857** | gpt-5.4 | 5 | Submitted | **PASS** |
| **riot__riot-5323** | Deepseek-V3 | 22 | Submitted | **PASS** |
| **riot__riot-5323** | Qwen3-Coder | 50 | LimitsExceeded | **FAIL** |
| **riot__riot-5323** | claude-opus-4-6 | 11 | Submitted | **PASS** |
| **riot__riot-5323** | gemini-2.5-pro | 7 | Submitted | **PASS** |
