# PRD — Surf Coaching Platform (User Stories & Acceptance Criteria)

## 1. Gestão de Perfil

### User Story 1.1 — Criação de perfil
Como usuário  
Quero criar uma conta com meu perfil  
Para acessar a plataforma

**Acceptance Criteria:**
- Usuário deve informar:
  - Email
  - Senha
  - Tipo de perfil (Surfista, Coach, Personal, Fotógrafo)
- Sistema deve validar unicidade de email
- Após cadastro, usuário deve estar autenticado

---

### User Story 1.2 — Configuração de perfil (Surfista)
Como surfista  
Quero configurar meus dados físicos  
Para receber análises e recomendações mais precisas

**Acceptance Criteria:**
- Campos disponíveis:
  - Cidade (obrigatório)
  - Idade (obrigatório)
  - Altura (opcional)
  - Peso (opcional)
  - Nível de surf (obrigatório)
- Dados devem ser editáveis a qualquer momento
- Dados devem ser utilizados nas análises e recomendações

---

## 2. Sessões de Surf

### User Story 2.1 — Criar sessão
Como surfista  
Quero registrar uma sessão de surf  
Para acompanhar minha evolução

**Acceptance Criteria:**
- Campos obrigatórios:
  - Data
  - Local
  - Condições do mar
  - Nível do surfista
- Campos opcionais:
  - Tipo de prancha
  - Observações
- Sessão deve ser salva com ID único

---

### User Story 2.2 — Upload de mídia
Como surfista  
Quero enviar fotos ou vídeos da sessão  
Para receber análise

**Acceptance Criteria:**
- Deve permitir:
  - Upload de múltiplas imagens
  - Upload de vídeos curtos
- Arquivos devem ser armazenados com referência à sessão
- Deve exibir progresso de upload

---

## 3. Análise de Performance

### User Story 3.1 — Análise por IA
Como surfista  
Quero receber uma análise automática  
Para entender meu desempenho

**Acceptance Criteria:**
- Sistema deve processar mídia enviada
- Deve gerar:
  - Texto descritivo
  - Scores (0–10)
  - Lista de melhorias
- Resultado deve ser salvo e vinculado à sessão

---

### User Story 3.2 — Análise por coach
Como surfista  
Quero solicitar análise de um coach  
Para obter feedback especializado

**Acceptance Criteria:**
- Usuário deve selecionar um coach
- Coach deve visualizar sessão e mídia
- Coach deve adicionar:
  - Texto
  - Scores
  - Sugestões
- Sistema deve armazenar análise separadamente da IA

---

### User Story 3.3 — Histórico de análises
Como surfista  
Quero ver minhas análises ao longo do tempo  
Para acompanhar evolução

**Acceptance Criteria:**
- Deve listar análises por sessão
- Deve permitir visualizar scores históricos
- Dados devem ser comparáveis entre sessões

---

## 4. Treinos

### User Story 4.1 — Receber treino sugerido
Como surfista  
Quero receber um treino baseado na minha análise  
Para melhorar pontos específicos

**Acceptance Criteria:**
- Sistema deve gerar treino baseado em:
  - Scores
  - Pontos de melhoria
- Treino deve conter:
  - Lista de exercícios
  - Séries e repetições

---

### User Story 4.2 — Visualizar exercícios
Como surfista  
Quero entender como executar os exercícios  
Para realizar corretamente

**Acceptance Criteria:**
- Exercício deve conter:
  - Descrição textual (obrigatório)
  - Vídeo demonstrativo (opcional)
- Interface deve exibir ambos quando disponíveis

---

### User Story 4.3 — Plano de treino
Como surfista  
Quero seguir um plano de treino estruturado  
Para manter consistência

**Acceptance Criteria:**
- Plano deve conter múltiplos treinos (ex: 3)
- Treinos devem ser rotacionáveis
- Sistema deve evitar repetição excessiva

---

### User Story 4.4 — Treinos por profissionais
Como surfista  
Quero escolher treinos criados por profissionais  
Para seguir orientações confiáveis

**Acceptance Criteria:**
- Treinos devem indicar autor (coach/personal)
- Usuário deve conseguir selecionar e aplicar treino
- Treinos devem ser associados ao perfil do profissional

---

## 5. Recomendação de Prancha

### User Story 5.1 — Receber recomendação
Como surfista  
Quero receber sugestão de prancha  
Para melhorar minha performance

**Acceptance Criteria:**
- Recomendação deve estar dentro da sessão
- Deve considerar:
  - Dados do perfil (altura, peso)
  - Nível
  - Performance na sessão
- Output deve incluir:
  - Tipo de prancha
  - Medidas
  - Volume estimado

---

### User Story 5.2 — Exportar recomendação
Como surfista  
Quero compartilhar a recomendação  
Para conversar com um shaper

**Acceptance Criteria:**
- Sistema deve permitir copiar/exportar dados
- Dados devem estar em formato claro e estruturado

---

## 6. Marketplace de Profissionais

### User Story 6.1 — Buscar coaches
Como surfista  
Quero encontrar coaches  
Para solicitar análises

**Acceptance Criteria:**
- Lista deve permitir filtros:
  - Localização
  - Especialidade
  - Avaliação
- Sistema deve ordenar por relevância

---

### User Story 6.2 — Selecionar coach
Como surfista  
Quero visualizar detalhes de um coach  
Para decidir contratar

**Acceptance Criteria:**
- Perfil deve exibir:
  - Descrição
  - Avaliações
  - Histórico de análises
- Deve permitir solicitar análise

---

### User Story 6.3 — Buscar personal trainers
Como surfista  
Quero encontrar profissionais de treino físico  
Para melhorar meu desempenho

**Acceptance Criteria:**
- Filtros:
  - Tipo de treino
  - Experiência
- Deve permitir visualizar e selecionar planos

---

### User Story 6.4 — Buscar fotógrafos
Como surfista  
Quero encontrar fotógrafos na minha região  
Para registrar minhas sessões

**Acceptance Criteria:**
- Filtros:
  - Localização
  - Spot
- Perfil deve conter:
  - Portfólio
  - Disponibilidade

---

### User Story 6.5 — Agendar fotógrafo
Como surfista  
Quero marcar uma sessão com fotógrafo  
Para obter mídia

**Acceptance Criteria:**
- Usuário deve selecionar data
- Sistema deve registrar intenção de booking
- Sessão pode ser vinculada posteriormente

---

## 7. Compartilhamento de Perfil

### User Story 7.1 — Compartilhar perfil
Como surfista  
Quero compartilhar meu perfil com um profissional  
Para receber acompanhamento

**Acceptance Criteria:**
- Deve permitir selecionar profissional
- Deve conceder acesso a:
  - Sessões
  - Análises
- Compartilhamento deve ser opcional

---

### User Story 7.2 — Revogar acesso
Como surfista  
Quero remover acesso ao meu perfil  
Para controlar minha privacidade

**Acceptance Criteria:**
- Usuário deve conseguir revogar acesso a qualquer momento
- Profissional perde acesso imediatamente

---

## 8. Regras Gerais

- Usuário possui apenas um perfil
- Altura e peso pertencem ao perfil, não à sessão
- Sessão é entidade central do sistema
- Análises são vinculadas à sessão
- Treinos derivam de análises
- Recomendação de prancha pertence à sessão
- Exercícios podem ser texto ou texto + vídeo