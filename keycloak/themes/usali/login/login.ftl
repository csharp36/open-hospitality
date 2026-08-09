<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username','password') displayInfo=realm.password && realm.registrationAllowed && !registrationDisabled??; section>
  <#if section = "header">
    Welcome back
  <#elseif section = "form">
    <#-- With Organizations on, the identity-first flow reaches THIS template
         for step two with `usernameHidden` set — so the card must say who it
         is about, or the screen is a bare password box with no subject. -->
    <p class="card-sub">
      <#if usernameHidden?? && auth?has_content && auth.showUsername()>
        Signing in as <strong>${auth.attemptedUsername}</strong>.
      <#else>
        Enter your credentials to access your concierge dashboard.
      </#if>
    </p>

    <#if realm.password>
      <form id="kc-form-login" action="${url.loginAction}" method="post"
            onsubmit="document.getElementById('kc-login').disabled = true; return true;">

        <#if !usernameHidden??>
          <div class="field">
            <label class="field-label" for="username">
              <#if !realm.loginWithEmailAllowed>Username<#elseif !realm.registrationEmailAsUsername>Username or email<#else>Email</#if>
            </label>
            <div class="control">
              <input id="username" name="username" type="text"
                     value="${(login.username!'')}"
                     placeholder="e.g. concierge@luxury-hotel.com"
                     autofocus autocomplete="username"
                     aria-invalid="<#if messagesPerField.existsError('username','password')>true</#if>">
              <span class="control-icon" aria-hidden="true">
                <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
                  <circle cx="10" cy="10" r="4" stroke="currentColor" stroke-width="1.5"/>
                  <path d="M14 10v1.5a2 2 0 0 0 4 0V10a8 8 0 1 0-3.1 6.3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              </span>
            </div>
            <#if messagesPerField.existsError('username','password')>
              <span class="field-error" aria-live="polite">${kcSanitize(messagesPerField.getFirstError('username','password'))?no_esc}</span>
            </#if>
          </div>
        </#if>

        <div class="field">
          <label class="field-label" for="password">Password</label>
          <div class="control">
            <input id="password" name="password" type="password"
                   autocomplete="current-password"
                   aria-invalid="<#if messagesPerField.existsError('username','password')>true</#if>">
            <button class="control-icon control-icon-btn" type="button" id="toggle-password"
                    aria-label="Show password" aria-pressed="false">
              <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
                <path d="M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10z" stroke="currentColor" stroke-width="1.5"/>
                <circle cx="10" cy="10" r="2.4" stroke="currentColor" stroke-width="1.5"/>
              </svg>
            </button>
          </div>
        </div>

        <#if realm.rememberMe && !usernameHidden??>
          <label class="remember">
            <input name="rememberMe" type="checkbox" <#if login.rememberMe??>checked</#if>>
            <span>Keep me signed in for 30 days</span>
          </label>
        </#if>

        <input type="hidden" name="credentialId" value="<#if auth.selectedCredential?has_content>${auth.selectedCredential}</#if>">

        <button id="kc-login" name="login" type="submit" class="submit">
          <span>Sign in</span>
          <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true">
            <path d="M3 10h13m-5-5 5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>

        <#if realm.resetPasswordAllowed>
          <div class="forgot-row">
            <a class="field-link" href="${url.loginResetCredentialsUrl}">Forgot password?</a>
          </div>
        </#if>
      </form>
    </#if>

    <div class="card-foot">
      <span>Don't have an account?</span>
      <a class="cta-secondary" href="mailto:sales@example.com">Contact sales</a>
    </div>

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
