# PRD — Surf Coaching Platform (Lean MVP)

## 1. Product Overview

The Surf Coaching Platform MVP is a web application that enables surfers to upload media from their surf sessions and receive AI-generated performance feedback.

The goal of this MVP is to validate whether surfers find value in structured feedback and whether they return to track their progress over time.

---

## 2. Objectives

### Primary Objective
Validate that surfers:
- Want structured feedback on their sessions
- Understand and value performance scores
- Return to upload multiple sessions

### Secondary Objective
Establish a foundation for future features such as training plans, coaches, and equipment recommendations.

---

## 3. Scope

### In Scope
- User authentication
- Basic surfer profile
- Session creation
- Media upload (image or short video)
- AI-generated performance analysis
- Session history with results

### Out of Scope
- Human coaches
- Training plans (full system)
- Marketplace (photographers, trainers)
- Board recommendation system
- Social features
- Mobile app

---

## 4. User Persona

### Surfer (Primary User)
- Wants to improve performance
- Lacks structured feedback
- Open to using technology for improvement

---

## 5. Core User Flow

1. User signs up and creates a profile  
2. User creates a surf session  
3. User uploads media (video or images)  
4. User requests analysis  
5. System generates feedback  
6. User reviews results  
7. User returns to upload new sessions  

---

## 6. Features & Requirements

## 6.1 User Authentication

### Description
Allow users to create and access accounts.

### Requirements
- Email and password registration
- Login functionality
- Basic session management

---

## 6.2 User Profile

### Description
Surfer profile used to contextualize analysis and personalize the experience.

### Fields
- Email
- Password
- Surf level (required)
- Name (optional)
- Gender: Male / Female (optional)
- Birthday (optional)
- Avatar — stored in Supabase Storage, URL persisted on profile (optional)
- Height (optional)
- Weight (optional)

### Requirements
- User can update profile
- Data persists across sessions
- Avatar upload handled directly by the client via Supabase Storage SDK; backend stores the resulting URL

---

## 6.3 Surf Sessions

### Description
Users can create sessions to track surf activity.

### Fields
- Date (required)
- Location (required)
- Wave size in feet (required) — numeric, e.g. `4.5`
- Surfboard (optional) — selected from the user's board inventory
- Notes (optional)

### Requirements
- Each session must be uniquely identified
- Sessions are linked to a user
- If a surfboard is referenced, it must belong to the authenticated user
- Deleting a surfboard sets the session reference to null (does not delete the session)

---

## 6.2.1 Surfboard Inventory

### Description
Users maintain a personal inventory of surfboards to associate with sessions.

### Fields
- Board type: shortboard / longboard / funboard / bodyboard / other (required)
- Board size in feet (required)
- Volume in litres (optional)
- Label / nickname (optional)

### Requirements
- Users can add, edit, and delete boards
- A user can own multiple boards
- Board is linked to the user's profile

---

## 6.4 Media Upload

### Description
Users upload media for analysis.

### Requirements
- Accept:
  - 1 video OR up to 3 images
- Video duration limit (e.g., 15–20 seconds)
- Upload progress feedback
- Files stored and linked to session

---

## 6.5 AI Performance Analysis

### Description
Core feature: generate structured feedback from uploaded media.

### Output
- Short descriptive feedback (text)
- 4 performance scores (0–10):
  - Paddle & pop-up
  - Stance & balance
  - Wave selection & timing
  - Maneuvers
- 3 actionable improvement tips

### Requirements
- Analysis triggered after upload
- Results persisted in database
- Results linked to session

---

## 6.6 Session History

### Description
Allow users to review past sessions and analyses.

### Requirements
- List all sessions
- Display:
  - Date
  - Thumbnail
  - Overall score
- Ability to open session details
- Display full analysis

---

## 6.7 Simplified Training Suggestions

### Description
Provide lightweight actionable tips instead of full training plans.

### Format
- Text-only suggestions embedded in analysis

### Requirements
- Exactly 3 suggestions per analysis
- Suggestions must be actionable and simple

---

## 7. Non-Functional Requirements

### Performance
- Analysis should complete within acceptable time (target: < 30s)

### Scalability
- System should support asynchronous processing (future-ready)

### Storage
- Media stored efficiently (compression recommended)

### Security
- Secure authentication
- Protected user data

---

## 8. Data Model (High-Level)

### User / Profile
- id
- email
- password_hash
- surf_level
- name
- gender (male | female)
- birthday
- avatar_url
- height
- weight
- created_at
- updated_at

### Surfboard
- id
- profile_id (FK → Profile)
- board_type (shortboard | longboard | funboard | bodyboard | other)
- board_size (feet)
- volume (litres, optional)
- label (optional)
- created_at
- updated_at

### Session
- id
- user_id (FK → Profile)
- date
- location
- wave_size (feet, numeric)
- surfboard_id (FK → Surfboard, nullable, SET NULL on delete)
- notes
- created_at
- updated_at

### Media
- id
- session_id
- type (image | video)
- url
- created_at

### Review
- id
- session_id
- feedback_text
- score_paddle_popup
- score_stance_balance
- score_wave_selection
- score_maneuvers
- overall_score
- improvement_tips (array)
- created_at

---

## 9. Success Metrics

### Activation
- % of users who upload at least one session

### Engagement
- Average number of sessions per user

### Retention
- % of users returning within 7 days

### Qualitative Feedback
- User-reported usefulness of feedback

---

## 10. Risks

- AI feedback quality may not meet expectations
- Users may not return after first use
- Media upload friction may reduce engagement

---

## 11. Timeline (2–3 Weeks)

### Week 1
- Authentication
- Profile setup
- Session creation
- Media upload

### Week 2
- AI integration
- Analysis generation
- Data persistence

### Week 3
- Frontend UI
- Session history
- Testing and deployment

---

## 12. Future Considerations (Post-MVP)

- Human coach integration
- Structured training programs
- Marketplace for professionals
- Board recommendation engine
- Mobile application