const { createApp } = Vue

frappe.ready((event)=>{
  createApp({
    delimiters: ['[%', '%]'],
    data() {
      return {
          id: '',
          payment_data: {},
          gateway: '',
          showDiv: false,
          references: {}
      }
    },
    methods: {
      payWithPaystack(){
          let me = this;
          let handler = PaystackPop.setup({
              key: me.payment_data.public_key, // Replace with your public key
              amount: me.payment_data.payment_request.grand_total * 100,
              ref: me.payment_data.name+'='+me.payment_data.payment_request.reference_name+'='+Math.floor((Math.random() * 1000000000) + 1), // generates a pseudo-unique reference. Please replace with a reference you generated. Or remove the line entirely so our API will generate one for you
              currency: me.payment_data.currency,
              email: me.payment_data.email,
              metadata: me.payment_data.metadata,
              // label: "Optional string that replaces customer email"
              onClose: function(){
                  alert('Payment Terminated.');
              },
              callback: function(response){
                  response.gateway=me.payment_data.metadata.gateway;
                  frappe.call({
                      type: "POST",
                      method: "frappe_paystack.www.paystack.pay.index.verify_transaction",
                      args:{transaction:response},
                      callback: function(r) {
                        Swal.fire(
                          'Successful',
                          'Your payment was successful, we will issue you receipt shortly.',
                          'success'
                        )
                        setTimeout(()=>{
                          window.location.href = `/orders/${me.payment_data.metadata.reference_name}`
                        }, 10000);
                      }
                  });
              }
          });
  
          handler.openIframe();
      },
      getData(){
        let me = this;
        let queryString = window.location.search;
        let urlParams = new URLSearchParams(queryString);
        let references = {
          reference_doctype: urlParams.get('reference_doctype'),
          reference_docname: urlParams.get('reference_docname'),
          gateway: urlParams.get('gateway'),
          currency: urlParams.get('currency'),
          amount: urlParams.get('amount'),
          description: urlParams.get('description'),
          email: urlParams.get('payer_email'),
          customer: urlParams.get('payer_name'),
        }
        if (references.reference_doctype && references.reference_docname){
          if(references.reference_doctype=='Payment Request'){
            this.references = references;
            // get payment data
            frappe.call({
              method: "frappe_paystack.www.paystack.pay.index.get_payment_request",
              type: "POST",
              args: references,
              callback: function(r) {
                if(r.message.status=='Paid'){
                  frappe.throw('This order has been paid.');
                  // window.location.href = history.back();
                } else {
                  me.payment_data = r.message;
                  me.payWithPaystack();
                }
              },
              freeze: true,
              freeze_message: "Preparing payment",
              async: true,
            });
          } else {
            Swal('error', 'Invalid', 'Your payment request is invalid!');
          }
        } else {
          Swal('error', 'Invalid', 'Your payment request is invalid!');
        }       
      },
      formatCurrency(amount, currency){
          if(currency){
              return Intl.NumberFormat('en-US', {currency:currency, style:'currency'}).format(amount);
          } else {
              return Intl.NumberFormat('en-US').format(amount);
          }
      }
    },
    mounted(){
      this.getData();
    }
  }).mount('#app')
})
