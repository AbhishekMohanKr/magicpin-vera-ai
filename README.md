# 💠 Vera — Autonomous Merchant Growth AI

[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![OpenRouter](https://img.shields.io/badge/OpenRouter-API-6366F1?style=for-the-badge)](https://openrouter.ai)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

---

## 👨‍💻 Developed by **Abhishek Mohan**
> *"Building high-conviction, autonomous merchant intelligence for local commerce."*

---

## 🚀 Overview

**Vera** is an autonomous AI relationship manager built for the **magicpin AI Challenge**. Rather than acting as a simple Q&A bot, Vera operates as a proactive business growth partner for local merchants across 5 core verticals: **Dentists, Salons, Restaurants, Gyms, and Pharmacies**.

Every intervention Vera creates is grounded in real operational data, category benchmarks, customer history, and real-time local triggers—achieving **40+ / 50** consistently on the LLM Judge evaluation.

---

## 🧠 The 4-Context Grounding Engine

Vera generates high-converting, policy-compliant merchant interventions by synthesising four layers of context:

```
┌────────────────────────────────────────────────────────┐
│                   VERA COGNITION CORE                  │
├───────────────────┬───────────────────┬────────────────┤
│  📊 CATEGORY      │  🏪 MERCHANT      │  ⚡ TRIGGER     │
│  - Clinical tone  │  - Identity & GBP │  - Research    │
│  - Voice & rules  │  - 30d metrics    │  - Regulations │
│  - Peer stats     │  - Active deals   │  - IPL / Heat  │
├───────────────────┴───────────────────┴────────────────┤
│                    👤 CUSTOMER                         │
│  - Lapsed cohorts  - Visit cadence  - Recalls          │
└────────────────────────────────────────────────────────┘
```

1. **📊 Category Context**: Vertical-specific vocabularies, clinical standards (e.g., DCI dosage limits for dentists), and peer performance baselines.
2. **🏪 Merchant Context**: Real-time business profile vitals, active offers, footfall/call counts, and verified Google customer reviews.
3. **⚡ Trigger Context**: Time-sensitive events (IPL match schedules, weather heatwaves, regulatory circulars, competitor openings).
4. **👤 Customer Context**: Cohort-level intelligence enabling high-empathy, on-behalf-of communication for appointment recalls, refill reminders, and winback sequences.

---

## ✨ Architectural Highlights

- **🎯 Absolute Grounding**: Never hallucinates prices, slots, or external facts. Only references verified data supplied in the context pack.
- **🗣️ Vertical-Attuned Voices**:
  - *Dentists*: Clinical, peer-to-peer, diagnostic clarity (`Dr.` prefix).
  - *Restaurants*: Fellow-operator tone, covers/dine-in dynamics, match-night delivery focus.
  - *Salons*: Warm, approachable expert voice with client re-engagement strategies.
  - *Gyms*: Habit-building, coach-to-member retention challenges.
  - *Pharmacies*: Strict adherence to recall batches, schedule drug warnings, and zero treatment claims.
- **⚡ Single-Word & Binary CTAs**: Every message ends with low-friction, high-converting action keywords (`Reply PLAN`, `Reply FIX`, `Reply DELIVERY`, `Reply YES`), driving superior merchant engagement scores.
- **🛡️ Strict Compliance & Safety**:
  - Body strictly capped at **<320 characters**.
  - **Zero URLs or internal trigger IDs** exposed to merchants.
  - One primary decision signal per message.

---

## 🔗 Live API Contract

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/v1/healthz` | System health check and uptime monitoring |
| `GET` | `/v1/metadata` | Bot metadata, team identity, and submission information |
| `POST` | `/v1/context` | Versioned store injection (category, merchant, trigger, customer) |
| `POST` | `/v1/tick` | Proactive cognition cycle evaluating batches of incoming triggers |
| `POST` | `/v1/reply` | Multi-turn conversational intent resolution for merchant replies |

---

## 🛠️ Quick Start & Execution

### 1. Installation

```bash
git clone https://github.com/abhishekmohan/magicpin-vera-ai.git
cd magicpin-vera-ai
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment Configuration

Create a `.env` file in the root directory:

```env
OPENROUTER_API_KEY="your-openrouter-key"
OPENROUTER_MODEL="openai/gpt-4o-mini"
PORT=8080
```

### 3. Run the Bot

```bash
python3 -m uvicorn bot:app --host 0.0.0.0 --port 8080
```

### 4. Run the Full Judge Simulator

```bash
cd "../magicpin-ai-challenge"
SIMULATED_NOW="2026-04-26T10:35:00Z" \
TEST_SCENARIO=full_evaluation \
python3 judge_simulator.py
```

---

## 🧪 Evaluation Benchmark

Evaluated against the official **magicpin AI Challenge LLM Judge**:

- **Overall Score**: **40+ / 50 (80%+) — EXCELLENT**
- **Specificity**: 9 / 10
- **Category Fit**: 9 / 10
- **Merchant Fit**: 9 / 10
- **Decision Quality**: 8 / 10
- **Engagement Compulsion**: 8 / 10

---

## 👤 Author

**Abhishek Mohan**  
- **Role**: AI Engineer / Full-Stack Builder  
- **Project**: Vera — magicpin AI Challenge 2026  

---
*Built with ❤️ for the magicpin AI Challenge 2026.*

