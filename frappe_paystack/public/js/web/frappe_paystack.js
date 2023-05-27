frappe.ready(function(event){
    // check if we are in order page
    
    if(window.location.pathname.startsWith('/orders/')){
        let link = window.location.pathname;
        let split_link = link.split('/');
        if (split_link[2]){
            frappe.call({
                type: "POST",
                method: "frappe_paystack.api.v1.get_sales_order_status",
                args:doc_info,
                callback: function(r) {
                    if(r.message=='Paid'){
                        let payBTN = $('#pay-for-order')[0];
                        let payBTNParent = payBTN.parentNode;
                        payBTN.remove();
                        payBTNParent.innerHTML = `<button class="btn btn-success btn-sm" id="pay-for-order"> Paid </button>`;
                    }
                }
            });
        }
    }
});