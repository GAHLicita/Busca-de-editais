# Busca diária de editais — licenças de software

Todo dia, às **06:47 (horário de Brasília)**, o GitHub consulta automaticamente o
**PNCP — Portal Nacional de Contratações Públicas** e gera um relatório com os
**novos editais** cujo objeto envolve licenças/assinaturas de plataformas como
ChatGPT, Claude, Gemini, Perplexity, Google Workspace, Zoom, Microsoft 365 etc.

> Pela Lei 14.133/2021, todos os órgãos públicos (federais, estaduais e municipais)
> são obrigados a publicar suas contratações no PNCP, qualquer que seja o portal de
> disputa usado (Compras.gov.br, BLL, BNC, Licitanet, Portal de Compras Públicas etc.).
> Por isso, consultar o PNCP cobre praticamente todos os portais. O relatório traz o
> link do edital no PNCP **e** o link do portal de origem, onde a proposta é enviada.

## Como o relatório chega até você

1. **Issue no GitHub (automático, sem configuração):** cada relatório vira uma
   *issue* na aba **Issues** deste repositório, com a etiqueta `editais`. O GitHub
   envia um e-mail de aviso para quem acompanha o repositório (botão **Watch** →
   *All Activity*).
2. **E-mail formatado (opcional, recomendado):** configure os segredos abaixo e o
   relatório chega direto na sua caixa de entrada.
3. **Histórico:** todos os relatórios ficam salvos na pasta [`relatorios/`](relatorios/).

Nos dias sem nenhum edital novo com proposta aberta, **nenhum e-mail é enviado**
(o relatório do dia fica só na pasta `relatorios/`). Se a consulta ao PNCP falhar,
chega um e-mail avisando da falha.

O GitHub às vezes atrasa ou pula execuções agendadas. Por isso há horários
reserva (08:17, 10:17 e 13:17 de Brasília), que só rodam se a busca do dia ainda
não tiver sido feita. E como cada busca cobre os 2 últimos dias, um dia perdido é
recuperado no dia seguinte.

### Configurar o envio por e-mail (Gmail)

1. Na conta Google que vai **enviar** os e-mails, ative a verificação em duas etapas
   e crie uma **senha de app** em <https://myaccount.google.com/apppasswords>.
2. No GitHub, abra **Settings → Secrets and variables → Actions → New repository secret**
   e crie:

   | Nome            | Valor                                                     |
   |-----------------|-----------------------------------------------------------|
   | `SMTP_HOST`     | `smtp.gmail.com`                                          |
   | `SMTP_PORTA`    | `465`                                                     |
   | `SMTP_USUARIO`  | e-mail Gmail que envia (ex.: `empresa@gmail.com`)        |
   | `SMTP_SENHA`    | a senha de app de 16 letras gerada no passo 1             |
   | `EMAIL_DESTINO` | quem recebe (vários separados por vírgula)                |

## Como o filtro funciona

O objeto de cada contratação publicada é comparado com listas de termos (sem
diferenciar maiúsculas/minúsculas, acentos ou pontuação):

| Arquivo | Efeito |
|---|---|
| [`config/termos_principais.txt`](config/termos_principais.txt) | Marcas/plataformas que vocês fornecem e IA generativa → **prioridade ALTA** |
| [`config/termos_gerais.txt`](config/termos_gerais.txt) | Tipos de ferramenta ("inteligência artificial", "videoconferência", "design gráfico"…) → **prioridade MÉDIA** |
| [`config/termos_contexto.txt`](config/termos_contexto.txt) | Palavras de licenciamento ("licença", "assinatura", "subscrição"…): uma delas precisa aparecer junto |
| [`config/termos_exclusao.txt`](config/termos_exclusao.txt) | Descarta falsos positivos (ex.: "lente zoom", cursos e eventos sobre IA) |
| [`config/termos_exclusao_media.txt`](config/termos_exclusao_media.txt) | Descarta da prioridade média sistemas de gestão, desenvolvimento e customização |

O relatório traz só editais **com prazo de proposta ainda aberto**. Contratações
diretas sem disputa (o órgão já escolheu o fornecedor) aparecem numa seção à
parte, resumida, para acompanhamento de mercado.

Para acrescentar ou remover um termo, basta editar o arquivo pelo próprio site do
GitHub (ícone de lápis), um termo por linha, e salvar (*Commit changes*).

Em [`config/configuracoes.json`](config/configuracoes.json) é possível escolher as
modalidades consultadas (pregão, concorrência, dispensa, inexigibilidade,
credenciamento), quantos dias para trás consultar e se os editais de prioridade
média devem aparecer.

Um edital aparece **uma única vez**: os já informados ficam registrados em
`dados/editais_vistos.json` e não se repetem nos relatórios seguintes.

## Rodar agora (sem esperar o horário)

Aba **Actions → Busca diária de editais → Run workflow**.

- Marque **"Buscar TODOS os editais com proposta ainda aberta"** para um levantamento
  completo do que está aberto hoje no PNCP, com os critérios atuais.
- Ou informe quantos dias para trás consultar (ex.: `7`).

## Rodar no computador (opcional)

```bash
python3 scripts/buscar_editais.py --dias 7 --sem-email
```
