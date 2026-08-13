name: TikTok to Telegram

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: repost
  cancel-in-progress: false

jobs:
  repost:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run TikTok bot
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHANNEL_ID: ${{ secrets.CHANNEL_ID }}
          TIKTOK_USERS: ${{ secrets.TIKTOK_USERS }}
        run: python bot.py

      - name: Run YouTube bot
        if: always()
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          YT_CHANNELS: ${{ secrets.YT_CHANNELS }}
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
          MAX_AGE_DAYS: '7'
          SHORTS_AS_VIDEO: '1'
          MAX_HEIGHT: '1080'
        run: python youtube.py

      - name: Commit state
        if: always()
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state.json state_youtube.json || true
          git diff --quiet --cached || git commit -m "update state [skip ci]"
          git pull --rebase --autostash
          git push

      - name: Notify on failure
        if: failure()
        run: |
          curl -s -X POST "https://api.telegram.org/bot${{ secrets.BOT_TOKEN }}/sendMessage" \
            --data-urlencode "chat_id=${{ secrets.ALERT_CHAT }}" \
            --data-urlencode "text=⚠️ Бот упал. Лог: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" \
            > /dev/null
