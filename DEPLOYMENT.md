# Hosting EduConnect on Render

1. Push this project to a private GitHub repository. Do not commit `.env` or SMTP passwords.
2. Create a new **Blueprint** on Render and select the repository. Render will read `render.yaml` and create the PostgreSQL database and web service.
3. In the Blueprint setup screen, supply the requested SMTP values for the web service:

   - `EMAIL_HOST`
   - `EMAIL_HOST_USER`
   - `EMAIL_HOST_PASSWORD`
   - `DEFAULT_FROM_EMAIL`
   - `BOOTSTRAP_ADMIN_USERNAME`
   - `BOOTSTRAP_ADMIN_PASSWORD`

   `BOOTSTRAP_ADMIN_EMAIL` is optional. Use a strong, unique bootstrap password.

   Use a transactional email provider and verify the sender domain with that provider before going live.
4. Deploy. The web service applies migrations before deploy.
5. Add your custom domain. Set `CSRF_TRUSTED_ORIGINS` on the web service to its full HTTPS address, for example `https://educonnect.example.edu.ng`, and redeploy.
6. Test password-reset and lecturer-message emails. When you are ready to pay for automated class reminders, add a Render cron service that runs `python manage.py send_due_course_reminders` every minute.

## Important production notes

- The application uses PostgreSQL through `DATABASE_URL` in production. Do not use SQLite on Render.
- Class reminders are disabled in this deployment blueprint. When a cron service is added later, delivery uses private BCC batching and retries failed SMTP batches until the class begins.
- Static files are served by WhiteNoise. Uploaded files currently use local filesystem storage; connect a persistent disk or object storage provider before relying on uploaded course files, passport photos, or documents in production.
- Browser alerts require HTTPS, browser permission, and an open EduConnect page. Email remains the reliable notification channel when the browser is closed.
