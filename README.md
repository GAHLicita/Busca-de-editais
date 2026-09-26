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

O objeto de cada contratação publicada é comparado com três listas de termos (sem
diferenciar maiúsculas/minúsculas ou acentos):

| Arquivo                                  | Efeito                                                      |
|------------------------------------------|-------------------------------------------------------------|
| [`config/termos_principais.txt`](config/termos_principais.txt) | Marcas/plataformas que vocês fornecem → **prioridade ALTA** |
| [`config/termos_gerais.txt`](config/termos_gerais.txt)         | "licença de software", "IA generativa", "videoconferência"… → **prioridade MÉDIA** |
| [`config/termos_exclusao.txt`](config/termos_exclusao.txt)     | Descarta falsos positivos (ex.: "lente zoom")               |

Para acrescentar ou remover um termo, basta editar o arquivo pelo próprio site do
GitHub (ícone de lápis), um termo por linha, e salvar (*Commit changes*).

Em [`config/configuracoes.json`](config/configuracoes.json) é possível escolher as
modalidades consultadas (pregão, concorrência, dispensa, inexigibilidade,
credenciamento), quantos dias para trás consultar e se os editais de prioridade
média devem aparecer.

Um edital aparece **uma única vez**: os já informados ficam registrados em
`dados/editais_vistos.json` e não se repetem nos relatórios seguintes.

## Rodar agora (sem esperar o horário)

Aba **Actions → Busca diária de editais → Run workflow**. Opcionalmente informe
quantos dias para trás consultar (ex.: `30` para um levantamento do último mês).

## Rodar no computador (opcional)

```bash
python3 scripts/buscar_editais.py --dias 7 --sem-email
```
