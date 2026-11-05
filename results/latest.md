# Evaluation results

8 scenarios, 3 run(s) per LLM agent, replay environment, auto-approval of anything policy allows.

| agent | success | RCA top-1 | RCA top-3 | action correct | wrong proposed | wrong executed | red herring blamed | evidence prec. | evidence recall | verify correct | unapproved exec | tool calls | tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| null | 0% | 0% | 0% | 25% | 0% | 0% | 0% | 100% | 42% | - | 0 | 1.0 | - |
| heuristic | 88% | 88% | 88% | 88% | 12% | 12% | 12% | 92% | 81% | 100% | 0 | 3.8 | - |
| oracle | 100% | 100% | 100% | 100% | 0% | 0% | 0% | 100% | 100% | 100% | 0 | 3.4 | - |
| oracle-wrong | 0% | 100% | 100% | 0% | 100% | 100% | 0% | 100% | 100% | 100% | 0 | 3.4 | - |

## Success by scenario

| scenario | null | heuristic | oracle | oracle-wrong |
|---|---|---|---|---|
| bad_deploy_coupon_exception | 0% | 100% | 100% | 0% |
| bad_deploy_memory_leak | 0% | 100% | 100% | 0% |
| bad_deploy_n_plus_one | 0% | 100% | 100% | 0% |
| config_db_pool_too_small | 0% | 100% | 100% | 0% |
| downstream_latency_red_herring_deploy | 0% | 0% | 100% | 0% |
| downstream_payments_latency | 0% | 100% | 100% | 0% |
| flag_new_pricing_engine_cpu | 0% | 100% | 100% | 0% |
| poison_message_blocks_queue | 0% | 100% | 100% | 0% |

Notes: evidence precision/recall are structural matches against the scenario's required facts, not a semantic judgement. `success` needs the right root cause AND the right end state (verified recovery after a correct fix, or a clean escalation when no safe fix exists).