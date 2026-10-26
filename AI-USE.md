# AI Usage Disclosure

**Project:** RootRunner — Iterative DNS Resolver with Caching  
**Repository:** https://github.com/MaiCoderHoon/RootRunner

## 1. Overview

AI tools were used throughout the development of RootRunner as coding and learning assistants. Their use included generating code, exploring implementation approaches, debugging, reviewing code, understanding DNS protocol concepts, supporting testing, and improving project documentation.

AI assistance helped accelerate development while the team learned about the underlying concepts and worked to understand, verify, and improve the implementation.

## 2. Areas of AI Assistance

AI tools were used for the following activities:

- **Code generation and development:** Generating portions of the implementation, suggesting algorithms and approaches, and helping implement or refine functionality.
- **DNS protocol learning:** Understanding iterative resolution, DNS message parsing and encoding, referrals, glue records, CNAME chains, TTL-based caching, negative caching, and TCP fallback.
- **Debugging and code review:** Investigating errors, reviewing implementation logic, identifying potential edge cases, and evaluating possible fixes.
- **Testing and verification:** Exploring test scenarios, checking expected behaviour, investigating failures, and verifying implementation behaviour during development.
- **Performance experiments:** Assisting with the interpretation and presentation of measurements such as cold-cache and warm-cache latency, cache hit ratios, query counts, and comparisons with `dig +trace`.
- **Documentation:** Improving the README and other project documentation for clarity, structure, and technical presentation.

The extent of AI assistance varied across tasks and components. AI-generated suggestions were not necessarily adopted unchanged.

## 3. Learning and Verification

AI assistance was used alongside the team's learning and development process. Team members worked to understand the relevant DNS concepts, examine the generated or suggested code, investigate issues, and test the implementation.

AI-generated code can contain errors, overlook protocol edge cases, or behave differently from what its explanation suggests. Consequently, generated code and suggestions require critical review and verification.

The team remains responsible for understanding the submitted work, explaining its design decisions, identifying its limitations, and accurately reporting test results and experimental findings.

## 4. Core Implementation Disclosure

AI tools were used to generate substantial portions of the core DNS implementation, including functionality related to DNS message parsing and encoding, iterative resolution, and caching.

The team also used AI to assist with understanding these components, resolving implementation issues, and improving the code during development. The implementation was developed alongside efforts to learn the underlying protocol concepts and verify the system's behaviour.

This disclosure explicitly acknowledges AI's contribution to the core implementation. The team does not claim that all submitted code was independently authored without AI assistance.

## 5. Experimental Results and Limitations

AI assistance may help interpret experimental results, but reported measurements must come from actual executions. Test counts, latency measurements, cache statistics, query counts, and other quantitative claims should be supported by recorded results.

The team remains responsible for ensuring that experimental conclusions accurately reflect the observed behaviour of RootRunner and that the documented limitations correspond to the submitted implementation.

## 6. Declaration

This document records the team's use of AI for code generation, coding assistance, technical learning, debugging, code review, testing support, experimental analysis, and documentation.

The team acknowledges AI's contribution to the development process and accepts responsibility for the final submitted work, its verification, and the accuracy of this disclosure.
