# Input and output contracts

| Interface | Contract |
| --- | --- |
| Market results | [H1 report](H1_HYBRID_RESULT_CONTRACT.md), [schema](h1-hybrid-result-contract.schema.json) |
| Opportunity boundary | [H2 target universe](H2_TARGET_UNIVERSE_CONTRACT.md), [schema](h2-target-universe-result-contract.schema.json) |
| Career profile | [H3](H3_CAREER_PROFILE_CONTRACT.md), [schema](h3-career-profile.schema.json) |
| Manual vacancy review | [H4/H5](H4_H5_SINGLE_VACANCY_CONTRACT.md), [vacancy schema](h4-vacancy-evidence.schema.json), [review schema](h5-review-plan.schema.json) |
| Compensation | [Manual supplement](COMPENSATION_SUPPLEMENT_CONTRACT.md) |
| Role comparisons | [Radar R1](CAREER_RADAR_R1_CONTRACT.md), [taxonomy](CAREER_RADAR_ROLE_TAXONOMY.md) |
| Research | [Evidence stream](CAREER_RADAR_RESEARCH_STREAM_CONTRACT.md) |

`config/profile.example.yaml` is the legacy mention-comparison input.
`config/career-profile.example.yaml` is the distinct evidence-bearing H3 input.
Both are fictional. Decision chronology lives in [ADRs](adr/README.md).
