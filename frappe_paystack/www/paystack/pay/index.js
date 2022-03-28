Vue.createApp({
  template:`
  <div class="container">
    <div class="py-5 text-center">
      <img class="d-block mx-auto mb-4" src="https://fiverr-res.cloudinary.com/images/q_auto,f_auto/gigs/93790811/original/d659bf6ae224ded386238ebc8e0a77c406ff9730/integrate-paystack-payment-gateway.png" alt="" width="300" height="120">
      <h2>Checkout</h2>
      <!-- <p class="lead">Below is an example form built entirely with Bootstrap's form controls. Each disabled form group has a validation state that can be triggered by attempting to submit the form without completing it.</p> -->
    </div>

    <div class="row">

      <div class="col-md-12 order-md-1">
        <h4 class="mb-3">Billing Information</h4>
        <form class="needs-validation" novalidate action="void();">
          <div class="row">
            <div class="col-md-6 mb-3">
              <label for="firstName">Name</label>
              <input type="text" class="form-control" id="name" placeholder="" v-model="references.customer" disabled>
            </div>
            <div class="col-md-6 mb-3">
              <label for="lastName">Email</label>
              <input type="text" class="form-control" id="email" placeholder="" v-model="references.email" disabled>

            </div>
            <div class="col-md-6 mb-12">
              <label for="lastName">Desc.</label>
              <input type="text" class="form-control" id="desc" placeholder="" v-model="references.description" disabled>

            </div>
            <div class="col-md-6 mb-3">
              <label for="lastName">Order ID</label>
              <input type="text" class="form-control" id="order_id" placeholder="" v-model="references.reference_docname" disabled>

            </div>
            <div class="col-md-6 mb-3">
              <label for="lastName">Currency</label>
              <input type="text" class="form-control" id="currency" placeholder="" v-model="references.currency" disabled>

            </div>
            <div class="col-md-6 mb-3">
              <label for="lastName">Amount</label>
              <input type="text" class="form-control" id="amount" placeholder="" v-model="references.amount" disabled>

            </div>
          </div>

          <button class="btn btn-primary btn-lg btn-block" type="button"
          id="paynow" @click="getPayment">Pay Now</button>
        </form>
      </div>
    </div>

  </div>
  `,
  data() {
    return {
      references: {},
    }
  },
  mounted(){
    // get payment data
    $(document).ready(()=>{
      this.initialize();
    })
  },
  methods: {
    initialize(){
      this.getPayment();
    },
    getPayment(){
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
          this.getPaymentRequest(references);
        } else {
          popAlert('error', 'Invalid', 'Your payment request is invalid!');
        }
      } else {
        popAlert('error', 'Invalid', 'Your payment request is invalid!');
      }
    },
    getPaymentRequest(references){
        let me = this;
        frappe.call({
          method: "frappe_paystack.api.v1.get_payment_request",
          type: "POST",
          args: references,
          callback: function(r) {
            if(r.message.status=='Paid'){
              frappe.throw('This order has been paid.');
              window.location.href = history.back();
            } else {
              me.paystackStart(r.message);
            }
          },
          freeze: true,
          freeze_message: "Preparing payment",
          async: true,
      });
    },
    paystackStart(res){
      let href = `/orders/${res.metadata.reference_name}`
      let handler = PaystackPop.setup({
        key: res.key,
        email: res.email,
        amount: Number(res.amount),
        ref: res.ref,
        currency: res.currency,
        metadata:res.metadata,
        // label: "Optional string that replaces customer email"
        onClose: function(){
          frappe.msgprint(__("You cancelled the payment."));
          window.location.href = href;
        },
        callback: function(response){
          console.log(response)
          // complete payment
          // frappe.call({
          //     method: "frappe_paystack.api.v1.webhook", //dotted path to server method
          //     args: response,
          //     callback: function(r) {
          //         // code snippet
          //         // console.log(r);
          //     }
          // })
          let message = 'Payment complete! Reference: ' + response.reference;
          // alert(message);
          frappe.msgprint({
              title: __('Notification'),
              indicator: 'green',
              message: __('Your payment has been received and will be processed shortly.')
          });

          setTimeout(function(){
            window.location.href = history.back();
            // alert("Hello");
          }, 10000);
        }
      });
      handler.openIframe();
    },
    popAlert(icon, title, message){
      Swal.fire({
        icon: icon,
        title: title,
        text: message,
      })
    }
  }
}).mount('#app')
