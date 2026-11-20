# UMW-GBUS-562
Repository for GBUS 562 (AI Language Models)

## About

This repository contains coursework and materials from GBUS 562: AI Language Models at the University of Mary Washington. Content is organized into weekly folders, so each week's work is easy to find.

## Weekly Work

1. [week-01](week-01/): two browser-based strategy games, each built as a single HTML page.
   - **Connect Four**: play against a computer opponent at three difficulty levels, or take turns with a friend in two-player mode.
   - **Oxono**: move the X and O totems and place matching pieces to line up four of your colour or four of one symbol, against a computer opponent at three difficulty levels.
2. [week-02](week-02/): a Prompt Lab web app for testing prompts against Claude, built with Claude Code.
3. [week-03](week-03/): a RAG tool that answers questions from any folder of documents, built with Claude Code.
   - Defaults to Copperline Manufacturing's finance documents; `--docs` points it at another folder (each folder gets its own git-ignored Chroma database), and `--save` writes the retrieved chunks and answer to a text file.
   - Tested on Larkfield credit union documents; the test runs are in `week-03/runs/` and the results are in [writeup.md](week-03/writeup.md).
4. [week-04](week-04/): an AI agent that prepares replies to HR emails using tools from an MCP server, built with Claude Code.
5. [week-05](week-05/): a team of AI agents that prepares a bicycle maker's response to supplier emails, built with Claude Code.
   - A supply analyst (using MCP data tools), a supplier-reply writer, a customer-notice writer and a reviewer, run by two coordinators: a fixed pipeline and an orchestrator agent.
   - Tested on three Brookfield Bikes supplier emails; the test runs are in `week-05/runs/` and the comparison of the two coordinators is in [writeup.md](week-05/writeup.md).
