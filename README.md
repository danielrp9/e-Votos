# Sistema de Votação Online

Aplicação web desenvolvida em Django para a realização de eleições online, composta por **cabina de votação pública**, **disparo automatizado de convites via SMTP** e **painel administrativo auditável** para a comissão eleitoral.

---

## 1. Arquitetura de Segurança e Sigilo do Voto

O sistema foi projetado seguindo o princípio da **separação física entre a identificação do eleitor e a cédula depositada**, buscando conciliar o **sigilo do voto** e a **auditabilidade pública do resultado**.

### Garantias de Sigilo e Integridade
* **Desvinculação Total (Voto × Eleitor):**
  * A tabela de **Eleitores** registra apenas o status de participação (`ja_votou = True` e a data de comparecimento), sem armazenar em qual chapa ou candidato o eleitor votou.
  * A tabela de **Votos** registra exclusivamente a opção escolhida, a eleição e um **código de comprovante** (`UUID v4`) gerado aleatoriamente no momento do depósito da cédula.
  * **Não existe** chave estrangeira (`ForeignKey`), log de endereço IP, *user-agent* ou carimbo de horário exato ligando um eleitor ao registro do seu voto.
* **Autenticação em Dois Fatores (Link Exclusivo + Identificador):**
  * Cada eleitor apto recebe um `token_convite` único (UUID v4) no seu e-mail cadastrado.
  * Ao acessar o link, o token é validado, armazenado temporariamente na sessão criptografada do navegador (`request.session`) e removido da barra de endereço (`redirect` para `/votar/cabina/`), evitando exposição no histórico do navegador ou nos logs HTTP do servidor.
  * Para abrir a cédula, o sistema exige a confirmação do **identificador cadastral (matrícula/registro)** correspondente àquele convite.
* **Proteção contra Voto Duplo e Concorrência:**
  * A gravação do voto e a baixa de comparecimento ocorrem dentro de uma transação atômica com bloqueio de linha no banco de dados (`transaction.atomic` + `select_for_update`), impedindo que múltiplos cliques simultâneos ou abas abertas registrem mais de um voto para o mesmo eleitor.
* **Imutabilidade Pós-Abertura da Urna:**
  * Assim que uma eleição entra no status **Em Andamento** ou **Encerrada**, o sistema bloqueia a edição de datas/regras da eleição, a criação/alteração de chapas e a exclusão de eleitores ou edição de identificadores, impedindo manipulação do colégio eleitoral ou das candidaturas durante o pleito.
* **Blindagem Web (OWASP):**
  * Todas as consultas ao banco utilizam o ORM parametrizado do Django (proteção contra *SQL Injection*).
  * Escape automático de HTML em todos os templates (proteção contra *XSS*), tokens CSRF obrigatórios em todos os formulários POST, cookies `HttpOnly`, cabeçalhos `X-Frame-Options: DENY` e `nosniff`, além de sanitização contra *CSV/Formula Injection* nas planilhas importadas e exportadas.

---

## 2. Fluxo Passo a Passo da Eleição (Ciclo de Auditabilidade)

O comportamento do sistema divide-se em **quatro fases operacionais**:

### Fase 1: Preparação e Homologação (Status: `AGENDADA`)
1. A comissão cria a eleição no painel (`/painel/eleicoes/`), definindo título, descrição, nomes dos membros da comissão e horários oficiais de **início** e **encerramento**.
2. A comissão cadastra as chapas concorrentes (número, nome, slogan e foto) e importa a base de eleitores aptos (planilha CSV/XLSX com nome, identificador/matrícula e e-mail).
3. O sistema inicia o disparo automático dos convites em lotes via SMTP autenticado.

### Fase 2: Emissão da Zerésima (Antes da Abertura)
1. Minutos antes do horário de início da votação, a comissão e os fiscais das chapas acessam **Zerésima** (`/painel/zeresima/`).
2. O sistema emite um relatório oficial imprimível comprovando que **existem 0 votos registrados** na urna, listando todas as chapas homologadas e o total de eleitores aptos importados, com campos para assinatura presencial/digital da comissão e dos fiscais.

### Fase 3: Votação na Urna (Status: `EM_ANDAMENTO`)
1. No horário exato de início, a urna abre automaticamente.
2. O eleitor clica no link recebido em seu e-mail, digita seu **identificador (matrícula/registro)**, escolhe a chapa desejada (ou voto em branco/nulo, conforme configurado) e confirma o voto.
3. **Recibo de Votação:** Imediatamente após a confirmação, o sistema exibe um **Recibo de Votação** (com opção de copiar o código ou salvar em PDF) contendo o hash anônimo do comprovante (`codigo_comprovante`). Por medida de sigilo, o recibo não exibe o nome do eleitor nem a opção escolhida e não é enviado por e-mail.
4. Durante todo o período de votação, o painel exibe apenas o **comparecimento agregado** (quantos eleitores já votaram), mantendo os resultados parciais por chapa bloqueados para evitar boca de urna interna.

### Fase 4: Encerramento, Apuração e Boletim de Urna (Status: `ENCERRADA`)
1. Ao atingir o horário de encerramento, a urna fecha automaticamente e rejeita qualquer nova tentativa de voto.
2. A tela **Apuração** (`/painel/apuracao/`) é desbloqueada e exibe:
   * Totalização final de votos por chapa e percentuais.
   * **Conferência de Integridade:** Comprova matematicamente que `Total de Votos na Urna == Total de Eleitores na Lista de Comparecimento`.
   * **Lista de Votantes (CSV):** Relação nominal (Nome e Identificador/Matrícula) dos eleitores que compareceram, para conferência pública pelos fiscais.
   * **Boletim de Urna (Comprovantes Embaralhados):** Lista pública com todos os códigos de recibo registrados, **ordenados alfabeticamente pelo hash** (nunca pela ordem cronológica de votação). Qualquer eleitor pode buscar seu código na lista para confirmar que seu voto foi computado sem expor sua escolha.

---

## 3. Desenvolvimento Local

Requer **Python 3.11** ou superior.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver


# Sistema de Votação Online

Aplicação web desenvolvida em Django para a realização de eleições online, composta por **cabina de votação pública**, **disparo automatizado de convites via SMTP** e **painel administrativo auditável** para a comissão eleitoral.

---

## 1. Fluxo Visual da Arquitetura e Sigilo

O diagrama abaixo ilustra como o sistema separa a autenticação do eleitor do registro da cédula na urna, mantendo o sigilo do voto e permitindo a auditoria pública após o encerramento:

```mermaid
flowchart TD
    subgraph Preparacao["1. Preparação & Zerésima (AGENDADA)"]
        A[Comissão cria Eleição, Chapas e importa Eleitores] --> B[Disparo em lotes via SMTP]
        B --> C[Emissão da Zerésima: 0 votos registrados]
    end

    subgraph Votacao["2. Cabina de Votação (EM_ANDAMENTO)"]
        D[Eleitor recebe link exclusivo com token_convite] --> E[Validação do token na sessão + Redirect /votar/cabina/]
        E --> F[Confirmação do Identificador / Matrícula]
        F --> G{Transação Atômica}
        G -->|Tabela Eleitor| H[Marca ja_votou = True e data de comparecimento]
        G -->|Tabela Voto| I[Grava apenas Chapa + codigo_comprovante UUID v4]
        I --> J[Exibe Recibo de Votação em PDF sem identificar chapa/eleitor]
    end

    subgraph Apuracao["3. Apuração & Auditoria (ENCERRADA)"]
        K[Encerramento automático no horário limite] --> L[Totalização de votos por chapa]
        L --> M[Lista nominal de comparecimento CSV]
        L --> N[Boletim de Urna: comprovantes ordenados alfabeticamente]
        M & N --> O[Conferência: Total de Votos == Total de Votantes]
    end

    C --> D
    J --> K