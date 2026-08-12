(()=>{frappe.ready(()=>{let e=document.getElementById("pay-for-order");if(!e)return;let t=!1;e.addEventListener("click",a=>{if(t){a.preventDefault();return}t=!0,e.classList.add("disabled"),e.setAttribute("aria-disabled","true"),e.textContent=__("Redirecting to payment...")})});})();
//# sourceMappingURL=paystack_cart_guard.bundle.GLDSJBCC.js.map
