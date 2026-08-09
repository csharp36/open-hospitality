<#--
  Step TWO of the identity-first browser flow: the password, once the username
  is known.

  The show/hide toggle is our own three-line script rather than base's
  `js/passwordVisibility.js`: that file lives in the BASE theme's resources,
  and `url.resourcesPath` resolves to OURS — so referencing it renders a
  toggle button wired to a 404.
-->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('password'); section>
  <#if section = "header">
    Welcome back
  <#elseif section = "form">
    <p class="card-sub">
      <#if auth?has_content && auth.showUsername() && !auth.showResetCredentials()>
        Signing in as <strong>${auth.attemptedUsername}</strong>.
      <#else>
        Enter your password to continue.
      </#if>
    </p>

    <form id="kc-form-login" action="${url.loginAction}" method="post"
          onsubmit="document.getElementById('kc-login').disabled = true; return true;">
      <div class="field">
        <label class="field-label" for="password">Password</label>
        <div class="control" dir="ltr">
          <input id="password" name="password" type="password"
                 autocomplete="current-password" autofocus tabindex="2"
                 aria-invalid="<#if messagesPerField.existsError('password')>true</#if>">
          <button class="control-icon control-icon-btn" type="button" id="toggle-password"
                  aria-label="Show password" aria-controls="password" aria-pressed="false">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
              <path d="M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10z" stroke="currentColor" stroke-width="1.5"/>
              <circle cx="10" cy="10" r="2.4" stroke="currentColor" stroke-width="1.5"/>
            </svg>
          </button>
        </div>
        <#if messagesPerField.existsError('password')>
          <span class="field-error" id="input-error-password" aria-live="polite">
            ${kcSanitize(messagesPerField.get('password'))?no_esc}
          </span>
        </#if>
      </div>

      <input type="hidden" name="credentialId"
             value="<#if auth.selectedCredential?has_content>${auth.selectedCredential}</#if>">

      <button id="kc-login" name="login" type="submit" class="submit" tabindex="4">
        <span>Sign in</span>
        <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true">
          <path d="M3 10h13m-5-5 5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <#if realm.resetPasswordAllowed>
        <div class="forgot-row">
          <a class="field-link" href="${url.loginResetCredentialsUrl}" tabindex="5">Forgot password?</a>
        </div>
      </#if>
    </form>

    <script>
      (function () {
        var btn = document.getElementById('toggle-password');
        var pw = document.getElementById('password');
        if (!btn || !pw) return;
        btn.addEventListener('click', function () {
          var show = pw.type === 'password';
          pw.type = show ? 'text' : 'password';
          btn.setAttribute('aria-pressed', String(show));
          btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
        });
      })();
    </script>
  </#if>
</@layout.registrationLayout>
