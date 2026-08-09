<#--
  Step ONE of the identity-first browser flow: username only, password on the
  next screen.

  This flow is what Keycloak switches to once the realm has Organizations
  (Pillar L), which is why it appeared without anyone choosing it. Base ships
  its own `login-username.ftl`, so with no override here the shell rendered
  from OUR template.ftl while the form inside came from base — a styled hero
  around a raw HTML input. The single-page `login.ftl` stays for realms
  without Organizations; both are reachable and both must look the same.
-->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username') displayInfo=(realm.password && realm.registrationAllowed && !registrationDisabled??); section>
  <#if section = "header">
    Welcome back
  <#elseif section = "form">
    <p class="card-sub">Enter your username to continue to your concierge dashboard.</p>

    <#if realm.password>
      <form id="kc-form-login" action="${url.loginAction}" method="post"
            onsubmit="document.getElementById('kc-login').disabled = true; return true;">

        <#if !usernameHidden??>
          <div class="field">
            <label class="field-label" for="username">
              <#if !realm.loginWithEmailAllowed>Username<#elseif !realm.registrationEmailAsUsername>Username or email<#else>Email</#if>
            </label>
            <div class="control">
              <input id="username" name="username" type="text" dir="ltr"
                     value="${(login.username!'')}"
                     placeholder="e.g. concierge@luxury-hotel.com"
                     autofocus autocomplete="username" tabindex="1"
                     aria-invalid="<#if messagesPerField.existsError('username')>true</#if>">
              <span class="control-icon" aria-hidden="true">
                <svg viewBox="0 0 20 20" width="18" height="18" fill="none">
                  <circle cx="10" cy="10" r="4" stroke="currentColor" stroke-width="1.5"/>
                  <path d="M14 10v1.5a2 2 0 0 0 4 0V10a8 8 0 1 0-3.1 6.3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              </span>
            </div>
            <#if messagesPerField.existsError('username')>
              <span class="field-error" id="input-error-username" aria-live="polite">
                ${kcSanitize(messagesPerField.get('username'))?no_esc}
              </span>
            </#if>
          </div>
        </#if>

        <#if realm.rememberMe && !usernameHidden??>
          <label class="remember">
            <input id="rememberMe" name="rememberMe" type="checkbox" tabindex="3"
                   <#if login.rememberMe??>checked</#if>>
            <span>Keep me signed in for 30 days</span>
          </label>
        </#if>

        <button id="kc-login" name="login" type="submit" class="submit" tabindex="4">
          <span>Continue</span>
          <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true">
            <path d="M3 10h13m-5-5 5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </form>
    </#if>

    <div class="card-foot">
      <span>Don't have an account?</span>
      <a class="cta-secondary" href="mailto:sales@example.com">Contact sales</a>
    </div>
  </#if>
</@layout.registrationLayout>
